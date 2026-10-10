"""Isolated filesystem stand-in for S3 projects and an explicit test source."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from .service import ProjectService, alignment_warnings, digest, filename_for_axis, now


PART = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
KEY = re.compile(r"(?:media|submissions)/[A-Za-z0-9_./-]{1,500}\Z")
VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".webm"}


class MockS3Catalog:
    """Expose one local test folder through S3-style prefix navigation."""

    def __init__(self, source_dir: Path, prefix: str):
        if (not source_dir.is_dir() or source_dir.is_symlink() or not prefix
                or any(not PART.fullmatch(part) or part in {".", ".."} for part in prefix.split("/"))):
            raise ValueError("Invalid S3 mock test folder or object prefix")
        self.source_dir = source_dir.resolve(strict=True)
        self.prefix = prefix
        self.parts = prefix.split("/")

    def browse(self, raw_prefix: str = "") -> dict:
        if raw_prefix and (raw_prefix.startswith("/") or raw_prefix.endswith("/") or ".." in raw_prefix.split("/")):
            raise HTTPException(422, "模拟存储目录无效。")
        parts = raw_prefix.split("/") if raw_prefix else []
        if parts != self.parts[:len(parts)]:
            raise HTTPException(404, "模拟存储目录不存在。")
        entries = []
        if len(parts) < len(self.parts):
            name = self.parts[len(parts)]
            entries.append({"name": name, "path": "/".join(parts + [name]), "kind": "directory"})
        else:
            for item in self.source_dir.iterdir():
                if item.is_symlink() or not item.is_file() or item.suffix.casefold() not in VIDEO_SUFFIXES:
                    continue
                entries.append({"name": item.name, "path": f"{self.prefix}/{item.name}",
                                "kind": "file", "size": item.stat().st_size})
            entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
        return {"root": "", "path": raw_prefix, "parent": "/".join(parts[:-1]) if parts else None,
                "entries": entries, "page": 0, "has_more": False, "can_open": len(parts) == len(self.parts)}

    def project_directory(self, prefix: str) -> Path:
        if prefix != self.prefix or not self.source_dir.is_dir() or self.source_dir.is_symlink():
            raise HTTPException(404, "请选择完整的模拟项目文件夹。")
        if any(item.is_symlink() and item.suffix.casefold() in VIDEO_SUFFIXES for item in self.source_dir.iterdir()):
            raise HTTPException(422, "测试视频不能是外部链接。")
        return self.source_dir


class MockS3Store:
    def __init__(self, root: Path):
        self.root = root / ".local" / "mock-s3"
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        secret_path = self.root / "signing-key"
        try:
            with secret_path.open("xb") as handle:
                os.chmod(secret_path, 0o600)
                handle.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        self.secret = secret_path.read_bytes()
        if len(self.secret) != 32:
            raise RuntimeError("Invalid mock object signing key")

    def _path(self, key: str) -> Path:
        if not KEY.fullmatch(key) or ".." in Path(key).parts:
            raise HTTPException(404, "对象不存在。")
        target = self.objects / key
        if target.is_symlink() or not target.resolve().is_relative_to(self.objects.resolve()):
            raise HTTPException(404, "对象不存在。")
        return target

    def put_file(self, key: str, source: Path) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if self._file_digest(target) != self._file_digest(source):
                raise HTTPException(409, "mock 对象版本已有不同内容。")
            return
        temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with source.open("rb") as inp, temporary.open("xb") as out:
                shutil.copyfileobj(inp, out, length=1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _file_digest(path: Path) -> str:
        checksum = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                checksum.update(chunk)
        return checksum.hexdigest()

    def put_submission(self, project_id: str, save_id: str, documents: dict[str, bytes]) -> list[str]:
        keys = []
        for axis, payload in documents.items():
            key = f"submissions/{project_id}/{save_id}/{filename_for_axis(axis)}"
            target = self._path(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    with temporary.open("xb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            if digest(target.read_bytes()) != digest(payload):
                raise HTTPException(503, "mock 标注文件读回不一致。")
            keys.append(key)
        pointer = self._path(f"submissions/{project_id}/latest.json")
        temporary = pointer.with_name("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(json.dumps({"save_id": save_id, "keys": keys}).encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, pointer)
        finally:
            temporary.unlink(missing_ok=True)
        return keys

    def signed_url(self, origin: str, key: str, *, lifetime: int = 300) -> str:
        self._path(key)
        expires = int(time.time()) + lifetime
        signature = hmac.new(self.secret, f"GET\n{key}\n{expires}".encode(), hashlib.sha256).hexdigest()
        return f"{origin}/mock-objects/{quote(key)}?expires={expires}&signature={signature}"

    def authorized_path(self, key: str, expires: int, signature: str) -> Path:
        target = self._path(key)
        if expires < int(time.time()) or expires > int(time.time()) + 3600:
            raise HTTPException(403, "mock 下载链接已过期。")
        expected = hmac.new(self.secret, f"GET\n{key}\n{expires}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(403, "mock 下载链接无效。")
        if not target.is_file():
            raise HTTPException(404, "对象不存在。")
        return target

    def media_manifest(self, service: ProjectService, manifest: dict, origin: str) -> dict:
        project_id, version = manifest["project_id"], manifest["version"]
        videos = []
        for video in manifest["videos"]:
            ident = video["id"]
            def asset(kind: str, suffix: str, index: int | None = None) -> str:
                source = service.sessions.asset(project_id, kind, ident, index, version=version)
                if source is None:
                    raise HTTPException(409, "mock 播放素材尚未准备完成。")
                key = f"media/{project_id}/{version}/{ident}/{suffix}"
                self.put_file(key, source)
                return self.signed_url(origin, key)
            sheets = [asset("sheet", f"sheet-{index}.jpg", index) for index in range(len(video["storyboard"]["sheets"]))]
            videos.append({**video, "url": asset("normal", "normal.mp4"),
                           "fast_url": asset("fast", "fast.mp4"),
                           "thumbnail_url": asset("thumbnail", "thumbnail.jpg"),
                           "storyboard": {**video["storyboard"], "sheets": sheets}})
        return {**manifest, "videos": videos}


class MockProjectService(ProjectService):
    def __init__(self, root: Path, store: MockS3Store):
        self.mock_store = store
        super().__init__(root, enable_remote=False)

    def public(self, project: dict) -> dict:
        result = super().public(project)
        if project.get("_mock_s3"):
            result["source_dir"] = None
            result["storage_mode"] = "s3-mock"
            result["storage_prefix"] = project.get("_mock_s3_key")
            result["cache_directories"] = []
            result["needs_source_relink"] = False
        return result

    def attach_source_caches(self, project: dict, *, video_ids: set[str] | None = None, migrate: bool = False) -> None:
        # The desktop test copy is read-only input. Generated media stays under the isolated mock app.
        project["_storage_version"] = 3
        project["_cache_warnings"] = []

    def writeback(self, ident: str, *, expected_revision: int | None = None) -> dict:
        with self.lock:
            project = self.load(ident)
            if not project.get("_mock_s3"):
                raise HTTPException(409, "此项目不属于 S3 mock。")
            if expected_revision is not None and project["revision"] != expected_revision:
                raise HTTPException(409, "项目已更新，请重新打开后提交。")
            save_id, documents = self.export_documents(project, for_writeback=True)
            self.verify_sources(project)
            keys = self.mock_store.put_submission(ident, save_id, documents)
            project["draft_dirty"] = False
            project["warnings"] = alignment_warnings(project["videos"])
            project["last_writeback"] = {"save_id": save_id, "saved_at": now()}
            project["updated_at"] = now()
            self.save(project)
            return {"paths": keys, "save_id": save_id}
