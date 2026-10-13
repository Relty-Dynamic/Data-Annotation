"""Isolated filesystem stand-in for S3 projects and an explicit test source."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime
from typing import Protocol
from urllib.parse import quote

from fastapi import HTTPException

from .previews import run_ffmpeg
from .service import AXES, VIDEO_EXTENSIONS, ProjectService, alignment_warnings, digest, filename_for_axis, now, parse_recording_start


PART = re.compile(r"[\w.-]{1,120}\Z")
VIDEO_SUFFIXES = VIDEO_EXTENSIONS


def file_sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


class CaptureTarget(Protocol):
    def capture_exists(self, prefix: str) -> bool: ...
    def publish_capture(self, prefix: str, prepared: Path, manifest: dict, service: ProjectService) -> None: ...


class MockS3Catalog:
    """Browse published mock objects, with the original desktop fixture retained for older projects."""

    def __init__(self, store: "MockS3Store", source_dir: Path | None = None, prefix: str = ""):
        self.store = store
        if source_dir is not None and (not source_dir.is_dir() or source_dir.is_symlink() or not prefix
                or any(not PART.fullmatch(part) or part in {".", ".."} for part in prefix.split("/"))):
            raise ValueError("Invalid S3 mock test folder or object prefix")
        self.source_dir = source_dir.resolve(strict=True) if source_dir else None
        self.prefix = prefix if source_dir else ""
        self.parts = self.prefix.split("/") if self.prefix else []

    def browse(self, raw_prefix: str = "") -> dict:
        if raw_prefix and (raw_prefix.startswith("/") or raw_prefix.endswith("/") or ".." in raw_prefix.split("/")):
            raise HTTPException(422, "模拟存储目录无效。")
        parts = raw_prefix.split("/") if raw_prefix else []
        if len(parts) > 5 or any(not PART.fullmatch(part) or part in {".", ".."} for part in parts):
            raise HTTPException(422, "模拟存储目录无效。")
        entries: dict[str, dict] = {}
        if not parts:
            entries["daily"] = {"name": "daily", "path": "daily", "kind": "directory"}
        elif parts[0] == "daily":
            target = self.store.objects.joinpath(*parts)
            if target.is_dir() and not target.is_symlink() and target.resolve().is_relative_to(self.store.objects.resolve()):
                for item in target.iterdir():
                    if item.is_symlink() or item.name.startswith(".") or item.name in {"timeline", "manifest.json"}:
                        continue
                    entries[item.name] = {"name": item.name, "path": f"{raw_prefix}/{item.name}",
                                          "kind": "directory" if item.is_dir() else "file"}
                    if item.is_file():
                        entries[item.name]["size"] = item.stat().st_size
            if self.parts[:len(parts)] == parts and len(parts) < len(self.parts):
                name = self.parts[len(parts)]
                entries.setdefault(name, {"name": name, "path": "/".join(parts + [name]), "kind": "directory"})
            elif self.parts == parts and self.source_dir:
                for item in self.source_dir.iterdir():
                    if item.is_file() and not item.is_symlink() and item.suffix.casefold() in VIDEO_SUFFIXES:
                        entries[item.name] = {"name": item.name, "path": f"{self.prefix}/{item.name}",
                                              "kind": "file", "size": item.stat().st_size}
            elif not target.is_dir():
                raise HTTPException(404, "模拟存储目录不存在。")
        else:
            raise HTTPException(404, "模拟存储目录不存在。")
        listed = sorted(entries.values(), key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["name"]))
        manifest_path = self.store.objects / raw_prefix / "manifest.json" if len(parts) == 3 else None
        uploaded = bool(manifest_path and manifest_path.is_file())
        capture = None
        if uploaded:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            capture = {field: manifest.get(field) for field in
                       ("collector_name", "uploader_name", "uploaded_by_username", "note", "capture_date")}
        return {"root": "", "path": raw_prefix, "parent": "/".join(parts[:-1]) if parts else None,
                "entries": listed, "page": 0, "has_more": False,
                "can_open": uploaded or (bool(self.parts) and parts == self.parts), "capture": capture}

    def project_directory(self, prefix: str) -> Path:
        parts = prefix.split("/")
        if len(parts) == 3 and parts[0] == "daily" and all(PART.fullmatch(part) and part not in {".", ".."} for part in parts):
            target = self.store.objects / prefix
            if (target.is_dir() and not target.is_symlink() and target.resolve().is_relative_to(self.store.objects.resolve())
                    and (target / "manifest.json").is_file()):
                return target
        if prefix == self.prefix and self.source_dir and self.source_dir.is_dir() and not self.source_dir.is_symlink():
            if any(item.is_symlink() and item.suffix.casefold() in VIDEO_SUFFIXES for item in self.source_dir.iterdir()):
                raise HTTPException(422, "测试视频不能是外部链接。")
            return self.source_dir
        raise HTTPException(404, "请选择完整的模拟项目文件夹。")


class MockS3Store:
    def __init__(self, root: Path):
        self.root = root / ".local" / "mock-s3"
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.capture_lock = threading.RLock()
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
        parts = key.split("/")
        if (len(key.encode("utf-8")) > 1024 or len(parts) < 2 or parts[0] not in {"media", "submissions", "daily"}
                or any(not part or part in {".", ".."} or len(part) > 255 or any(ord(char) < 32 or char in "\\:" for char in part)
                       for part in parts)):
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

    def publish_capture(self, prefix: str, prepared: Path, manifest: dict, service: ProjectService) -> None:
        """Accept only verified playback objects at the mock storage boundary."""
        target = self._path(f"{prefix}/manifest.json").parent
        files = manifest.get("files")
        if (not isinstance(files, list) or not files or manifest.get("prefix") != prefix
                or any(not isinstance(item, dict) or not isinstance(item.get("key"), str)
                       or not re.fullmatch(r"FPV/[^/]+\.mp4", item["key"], re.IGNORECASE)
                       for item in files)):
            raise HTTPException(422, "压缩文件清单无效，未发布采集目录。")
        expected = {item["key"] for item in files}
        if len(expected) != len(files) or {path.relative_to(prepared).as_posix() for path in prepared.rglob("*") if path.is_file()} != expected:
            raise HTTPException(422, "压缩文件清单与成品不一致，未发布采集目录。")
        for item in files:
            video = prepared / item["key"]
            if (video.is_symlink() or not video.is_file() or video.stat().st_size != item.get("size")
                    or self._file_digest(video) != item.get("sha256") or service.probe(video)["codec"] != "h264"):
                raise HTTPException(422, "压缩视频校验失败，未发布采集目录。")
        (prepared / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        with self.capture_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise HTTPException(409, "采集目录已存在，未覆盖已有数据。")
            os.replace(prepared, target)

    def capture_exists(self, prefix: str) -> bool:
        return self._path(f"{prefix}/manifest.json").parent.exists()

    @staticmethod
    def _file_digest(path: Path) -> str:
        return file_sha256(path)

    def put_submission(self, project_id: str, save_id: str, documents: dict[str, bytes], prefix: str = "", *, available_axes: set[str] | None = None) -> list[str]:
        keys = [(f"{prefix}/timeline/{filename_for_axis(axis)}" if prefix else
                 f"submissions/{project_id}/{save_id}/{filename_for_axis(axis)}", payload)
                for axis, payload in documents.items()]
        pointer = self._path(f"{prefix}/timeline/.latest.json" if prefix else f"submissions/{project_id}/latest.json")
        pointer.parent.mkdir(parents=True, exist_ok=True)
        targets = {self._path(key): payload for key, payload in keys}
        if prefix:
            available = available_axes if available_axes is not None else set(AXES) | set(documents)
            for old in pointer.parent.glob("*.timeline.json"):
                if old.name.removesuffix(".timeline.json") not in available:
                    targets.setdefault(self._path(old.relative_to(self.objects).as_posix()), None)
        targets[pointer] = json.dumps({"save_id": save_id, "keys": [key for key, _ in keys]}).encode()
        previous = {path: path.read_bytes() if path.exists() else None for path in targets}
        try:
            for target, payload in targets.items():
                if payload is None:
                    target.unlink(missing_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
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
        except BaseException:
            for target, payload in previous.items():
                if payload is None:
                    target.unlink(missing_ok=True)
                else:
                    temporary = target.with_name("." + target.name + ".rollback.tmp")
                    temporary.write_bytes(payload)
                    os.replace(temporary, target)
            raise
        return [key for key, _ in keys]

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
        project = service.load(project_id)
        relative_paths = {video["id"]: video["relative_path"] for video in project["videos"]}
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
            normal_url = (self.signed_url(origin, f"{project['_mock_s3_key']}/{relative_paths[ident]}")
                          if project.get("_mock_s3_uploaded") else asset("normal", "normal.mp4"))
            videos.append({**video, "url": normal_url,
                           "fast_url": asset("fast", "fast.mp4"),
                           "thumbnail_url": asset("thumbnail", "thumbnail.jpg"),
                           "storyboard": {**video["storyboard"], "sheets": sheets}})
        return {**manifest, "videos": videos}


class MockUploadManager:
    """Receive source AVI on the uploader computer and hand only MP4 to the object store."""

    MAX_FILES = 1000
    MAX_FILE_BYTES = 16 * 1024**3
    MAX_TOTAL_BYTES = 100 * 1024**3

    def __init__(self, store: CaptureTarget, staging: Path):
        self.store = store
        self.staging = staging
        self.staging.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.selections: dict[str, dict] = {}

    @staticmethod
    def _relative_path(value: str) -> tuple[str, bool]:
        parts = value.split("/")
        if (not 1 <= len(parts) <= 8 or any(not part or part in {".", ".."} or len(part) > 120
                or any(ord(char) < 32 or char in "\\:" for char in part) for part in parts)):
            raise HTTPException(422, "上传文件的相对路径无效。")
        source = Path(parts[-1])
        if source.suffix.casefold() != ".avi":
            raise HTTPException(422, "本次仅上传 AVI 视频，不接收 TXT、IMU 或心率文件。")
        if len(parts) == 1 or (len(parts) == 2 and parts[0].casefold() in {"fpv", "video"}):
            return "FPV/" + source.stem + ".mp4", True
        raise HTTPException(422, "AVI 须来自 video 或 FPV 子目录，或直接选择视频文件。")

    def select_local(self, user_id: str, paths: list[str]) -> dict:
        """Keep native-picker paths server-side; the browser receives only names and a token."""
        if not paths or len(paths) > self.MAX_FILES:
            raise HTTPException(422, "请选择 1 到 1000 个 AVI 视频。")
        files, outputs, total, parent = [], set(), 0, None
        for raw in paths:
            source = Path(raw).expanduser()
            try:
                if source.is_symlink() or not source.is_file():
                    raise HTTPException(422, "所选视频不存在或是文件链接，请重新选择。")
                source = source.resolve(strict=True)
                stat = source.stat()
            except OSError as error:
                raise HTTPException(409, "采集设备已断开或视频不可读，请重新选择。") from error
            if source.parent.name.casefold() != "video" or (parent is not None and source.parent != parent):
                raise HTTPException(422, "请在同一个 video 文件夹中选择本次采集的 AVI。")
            parent = source.parent
            output, _ = self._relative_path(f"video/{source.name}")
            if output.casefold() in outputs:
                raise HTTPException(409, "压缩后的文件名重复，请先整理采集目录。")
            outputs.add(output.casefold())
            if not 0 < stat.st_size <= self.MAX_FILE_BYTES:
                raise HTTPException(422, "所选视频大小无效或超过单文件上限。")
            total += stat.st_size
            files.append({"source": str(source), "name": source.name, "size": stat.st_size,
                          "mtime_ns": stat.st_mtime_ns, "device": stat.st_dev, "inode": stat.st_ino,
                          "output": output,
                          "date": parse_recording_start(source.name).strftime("%Y%m%d")})
        if total > self.MAX_TOTAL_BYTES:
            raise HTTPException(422, "所选视频总量超过上限。")
        ident = uuid.uuid4().hex
        with self.lock:
            self.selections = {key: value for key, value in self.selections.items()
                               if time.time() - value["selected_at"] < 3600}
            self.selections[ident] = {"owner": user_id, "files": files, "selected_at": time.time()}
        return {"selection_id": ident, "files": [{"name": item["name"], "size": item["size"]} for item in files],
                "capture_date": min(item["date"] for item in files)}

    @staticmethod
    def _check_local_source(record: dict) -> Path:
        source = Path(record["source"])
        try:
            stat = source.stat()
        except OSError as error:
            raise HTTPException(409, "采集设备已断开或视频不可读，请重新连接后重试。") from error
        if (source.is_symlink() or not source.is_file() or stat.st_size != record["size"]
                or stat.st_mtime_ns != record["mtime_ns"] or stat.st_dev != record["device"]
                or stat.st_ino != record["inode"]):
            raise HTTPException(409, "所选 AVI 已变化，请重新选择后上传。")
        return source

    def start(self, user: dict, collector_name: str, uploader_name: str | None,
              note: str | None, selection_id: str) -> dict:
        collector_name = collector_name.strip()
        uploader_name = (uploader_name or "").strip() or user["display_name"]
        note = (note or "").strip() or None
        if (not 1 <= len(collector_name) <= 80 or not PART.fullmatch(collector_name)
                or collector_name in {".", ".."}):
            raise HTTPException(422, "采集人姓名只能包含文字、数字、下划线、点或短横线。")
        if not 1 <= len(uploader_name) <= 80 or any(ord(char) < 32 for char in uploader_name):
            raise HTTPException(422, "上传人姓名无效。")
        if note and (len(note) > 500 or any(ord(char) < 32 and char not in "\n\r\t" for char in note)):
            raise HTTPException(422, "备注无效或超过 500 字。")
        with self.lock:
            selection = self.selections.get(selection_id)
        if not selection or selection["owner"] != user["id"] or time.time() - selection["selected_at"] >= 3600:
            raise HTTPException(404, "视频选择已失效，请重新选择。")
        records = [{"index": index, "path": f"video/{item['name']}", **item}
                   for index, item in enumerate(selection["files"])]
        for record in records:
            self._check_local_source(record)
        capture_date = min(item["date"] for item in records)
        datetime.strptime(capture_date, "%Y%m%d")
        folder = f"{capture_date[4:]}{collector_name}"
        prefix = f"daily/{capture_date}/{folder}"
        with self.lock:
            if self.store.capture_exists(prefix):
                raise HTTPException(409, "此采集日期和个人文件夹已存在，不能覆盖已有素材或标注。")
            if any(job["prefix"] == prefix and job["state"] in {"ready_to_compress", "compressing"}
                   for job in self.jobs.values()):
                raise HTTPException(409, "此采集目录已有上传任务。")
            if shutil.disk_usage(self.staging).free < 512 * 1024**2:
                raise HTTPException(507, "本机空间不足，无法生成压缩视频。")
            ident = uuid.uuid4().hex
            (self.staging / ident).mkdir(parents=True)
            job = {"id": ident, "owner": user["id"], "prefix": prefix, "records": records,
                   "capture_date": capture_date, "collector_name": collector_name,
                   "uploader_name": uploader_name, "uploaded_by_username": user["username"], "note": note,
                   "state": "ready_to_compress", "progress": 0,
                   "detail": "已选择本机 AVI，等待压缩。", "updated_at": time.time()}
            self.jobs[ident] = job
            self.selections.pop(selection_id, None)
        return self.status(ident, user["id"])

    def _job(self, ident: str, user_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(ident)
            if not job or job["owner"] != user_id:
                raise HTTPException(404, "上传任务不存在。")
            return job

    def status(self, ident: str, user_id: str) -> dict:
        job = self._job(ident, user_id)
        with self.lock:
            return {"id": ident, "prefix": job["prefix"], "state": job["state"],
                    "progress": job["progress"], "detail": job["detail"],
                    "collector_name": job["collector_name"], "uploader_name": job["uploader_name"],
                    "uploaded_by_username": job["uploaded_by_username"], "note": job["note"],
                    "selected": len(job["records"]),
                    "total": len(job["records"])}

    def abort(self, ident: str, user_id: str) -> dict:
        job = self._job(ident, user_id)
        with self.lock:
            if job["state"] not in {"ready_to_compress", "error"}:
                raise HTTPException(409, "正在压缩或已发布的采集目录不能取消。")
            self.jobs.pop(ident, None)
        shutil.rmtree(self.staging / ident, ignore_errors=True)
        return {"ok": True}

    def _progress(self, job: dict, index: int, percent: float) -> None:
        bounded = max(0, min(100, percent))
        with self.lock:
            job.update(progress=round((index - 1 + bounded / 100) / len(job["records"]) * 99, 1),
                       detail=f"正在压缩第 {index}/{len(job['records'])} 个视频。", updated_at=time.time())

    def finish(self, ident: str, user_id: str, service: ProjectService, can_upload) -> dict:
        job = self._job(ident, user_id)
        with self.lock:
            if job["state"] not in {"ready_to_compress", "error"}:
                raise HTTPException(409, "上传任务当前不能开始压缩。")
            job.update(state="compressing", detail="正在本机压缩 FPV 视频并核对文件。")
        workspace = self.staging / ident
        publish = workspace / "publish"
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            publish.mkdir()
            for count, record in enumerate(job["records"], 1):
                if not can_upload(user_id):
                    raise HTTPException(403, "上传权限已关闭，未发布数据。")
                source = self._check_local_source(record)
                target = publish / record["output"]
                target.parent.mkdir(parents=True, exist_ok=True)
                self._compress_video(service, source, target, lambda percent, index=count: self._progress(job, index, percent))
                self._check_local_source(record)
                with self.lock:
                    job.update(progress=round(count / len(job["records"]) * 99, 1),
                               detail=f"已处理 {count}/{len(job['records'])} 个文件。")
            manifest = {"schema_version": 1, "prefix": job["prefix"], "uploaded_by": user_id,
                        "uploaded_by_username": job["uploaded_by_username"],
                        "collector_name": job["collector_name"], "uploader_name": job["uploader_name"],
                        "capture_date": job["capture_date"], "note": job["note"],
                        "created_at": now(), "video_profile": "playback-480x270-256k32k-v1",
                        "files": [{"source": item["path"], "key": item["output"],
                                   "size": (publish / item["output"]).stat().st_size,
                                   "sha256": file_sha256(publish / item["output"])} for item in job["records"]]}
            if not can_upload(user_id):
                raise HTTPException(403, "上传权限已关闭，未发布数据。")
            self.store.publish_capture(job["prefix"], publish, manifest, service)
            with self.lock:
                job.update(state="ready", progress=100, detail="压缩文件已发布到 S3 mock。")
            return self.status(ident, user_id)
        except BaseException:
            with self.lock:
                job.update(state="error", detail="上传或压缩失败，未发布采集目录。")
            raise
        finally:
            # Staging contains only this task's upload copies; never remove a published capture.
            shutil.rmtree(workspace, ignore_errors=True)

    @staticmethod
    def _compress_video(service: ProjectService, source: Path, target: Path, update) -> None:
        ffmpeg = service.tool("ffmpeg")
        if not ffmpeg:
            raise HTTPException(503, "缺少 FFmpeg，无法上传压缩视频。")
        original = service.probe(source)
        duration = original["duration_ms"]
        filters = ("setpts=PTS-STARTPTS,scale=480:270:force_original_aspect_ratio=decrease:reset_sar=1,"
                   "pad=480:270:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
        args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1",
                "-nostats", "-stats_period", "0.5", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
                "-vf", filters, "-c:v", "libx264", "-preset", "veryfast", "-b:v", "256k",
                "-maxrate", "320k", "-bufsize", "512k", "-g", "15", "-keyint_min", "15",
                "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-c:a", "aac", "-b:a", "32k",
                "-movflags", "+faststart", str(target)]
        try:
            run_ffmpeg(args, duration, target.with_suffix(".ffmpeg.log"), update, threading.Event())
            rendered = service.probe(target)
            if abs(rendered["duration_ms"] - duration) > 250 or abs(rendered["media_start_seconds"]) >= .001:
                raise HTTPException(422, "压缩视频时长或起点校验失败，未发布采集目录。")
        finally:
            target.with_suffix(".ffmpeg.log").unlink(missing_ok=True)


class MockProjectService(ProjectService):
    def __init__(self, root: Path, store: MockS3Store, nas_root: Path | None = None):
        self.mock_store = store
        self.nas_root = nas_root
        super().__init__(root, enable_remote=False)

    def public(self, project: dict) -> dict:
        result = super().public(project)
        if project.get("_mock_s3"):
            result["source_dir"] = None
            result["storage_mode"] = "s3-mock"
            result["storage_prefix"] = project.get("_mock_s3_key")
            result["cache_directories"] = []
            result["needs_source_relink"] = False
        elif project.get("source_dir"):
            result["storage_mode"] = "nas"
        return result

    def attach_source_caches(self, project: dict, *, video_ids: set[str] | None = None, migrate: bool = False) -> None:
        source_dir = project.get("source_dir")
        if self.nas_root and source_dir and Path(source_dir).is_relative_to(self.nas_root) and not project.get("_mock_s3"):
            super().attach_source_caches(project, video_ids=video_ids, migrate=migrate)
        else:
            # Mock objects and desktop test folders remain read-only inputs.
            project["_storage_version"] = 3
            project["_cache_warnings"] = []

    def writeback(self, ident: str, *, expected_revision: int | None = None) -> dict:
        if not self.load(ident).get("_mock_s3"):
            return super().writeback(ident, expected_revision=expected_revision)
        with self.lock:
            project = self.load(ident)
            if not project.get("_mock_s3"):
                raise HTTPException(409, "此项目不属于 S3 mock。")
            if expected_revision is not None and project["revision"] != expected_revision:
                raise HTTPException(409, "项目已更新，请重新打开后提交。")
            save_id, documents = self.export_documents(project, for_writeback=True)
            self.verify_sources(project)
            keys = self.mock_store.put_submission(ident, save_id, documents, project.get("_mock_s3_key", ""),
                                                  available_axes=set(AXES) | {track["id"] for track in project.get("custom_tracks", [])})
            project["draft_dirty"] = False
            project["warnings"] = alignment_warnings(project["videos"])
            project["last_writeback"] = {"save_id": save_id, "saved_at": now()}
            project["updated_at"] = now()
            self.save(project)
            return {"paths": keys, "save_id": save_id}
