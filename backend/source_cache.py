from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from pathlib import Path

from fastapi import HTTPException

from .project_storage import PROJECT_ID, checked_tree, link_copy

CACHE_FOLDER = ".datamark-cache"
MARKER = ".datamark-owner.json"
ROOT_OWNER = {"application": "DataMark", "kind": "source-preview-cache", "version": 1}
README = """# DataMark 视频预览缓存

这个文件夹由 DataMark 视频标注平台自动创建，用来保存当前目录原视频的普通预览、50/100 倍速预览、时间轴缩略图、悬停预览图片及相关处理状态。

- 原视频始终保留在原目录，不会复制进这个缓存文件夹，也不会因为删除项目而被删除。
- 每个项目使用独立的项目 ID 子文件夹；平台删除项目时，只删除属于该项目的缓存。
- 标注草稿保存在平台的本地数据库；另存或写回原目录的 JSON 结果不会随缓存删除。
- 完成的缓存会重复使用。手工删除缓存后需要重新生成；它不是原视频备份。
- 缓存与原视频所在磁盘一起使用。拔出 SD 卡或断开共享目录后，请先重新连接再使用。
- 不要在这里保存原视频或个人文件，不要改动 .datamark-owner.json 所有权标记。
"""


def is_reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name == "nt" else path.is_symlink()
    except FileNotFoundError:
        return False


def check_plain_tree(path: Path) -> None:
    if is_reparse(path):
        raise HTTPException(409, "缓存目录包含外部链接，已停止操作。")
    if path.exists():
        for current, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                if is_reparse(Path(current) / name):
                    raise HTTPException(409, "缓存目录包含外部链接，已停止操作。")


class SourceCache:
    """Only exact, marked project children may be created or removed beside originals."""

    @staticmethod
    def directory(source: Path, ident: str) -> Path:
        if not PROJECT_ID.fullmatch(ident):
            raise HTTPException(404, "找不到这个标注项目。")
        return source.parent / CACHE_FOLDER / ident

    @staticmethod
    def owner(ident: str) -> dict:
        return {**ROOT_OWNER, "project_id": ident}

    @staticmethod
    def _marked_directory(path: Path, owner: dict, create: bool) -> None:
        if is_reparse(path):
            raise HTTPException(409, "缓存目录包含外部链接，已停止操作。")
        if not path.exists():
            if not create:
                return
            path.mkdir()
            try:
                (path / MARKER).write_text(json.dumps(owner, ensure_ascii=False), encoding="utf-8")
            except BaseException:
                # Never remove a folder whose ownership marker could not be established.
                raise
        try:
            actual = json.loads((path / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(409, "原目录已有同名缓存文件夹，但缺少 DataMark 所有权标记，请更换目录或核实后重试。")
        if actual != owner or is_reparse(path / MARKER):
            raise HTTPException(409, "缓存目录的所有权标记不匹配，已停止操作。")
        if create and not (path / "README.md").exists():
            (path / "README.md").write_text(README + ("\n本目录项目 ID：`" + owner["project_id"] + "`\n" if "project_id" in owner else ""), encoding="utf-8")

    def validate(self, source: Path, ident: str, saved: str | None = None, *, create: bool = False) -> Path:
        parent = source.parent
        if not parent.is_dir():
            raise HTTPException(404, "原视频目录不可访问，请重新连接 SD 卡或共享目录后重试。")
        directory = self.directory(source, ident)
        if saved is not None and os.path.normcase(str(Path(saved))) != os.path.normcase(str(directory / "preview")):
            raise HTTPException(409, "缓存路径与原视频目录不匹配，已停止操作。")
        root = directory.parent
        if is_reparse(parent) or root.resolve().parent != parent.resolve() or directory.resolve().parent != root.resolve():
            raise HTTPException(409, "缓存路径超出原视频目录，已停止操作。")
        self._marked_directory(root, ROOT_OWNER, create)
        self._marked_directory(directory, self.owner(ident), create)
        preview = directory / "preview"
        if is_reparse(preview):
            raise HTTPException(409, "缓存目录包含外部链接，已停止操作。")
        if create:
            preview.mkdir(exist_ok=True)
            # A real write test rejects read-only cards/shares before starting import.
            probe = preview / (".write-check-" + uuid.uuid4().hex)
            try:
                with probe.open("xb") as handle:
                    handle.write(b"ok")
                probe.unlink()
            except OSError:
                probe.unlink(missing_ok=True)
                raise HTTPException(422, "原视频目录不可写，无法创建旁置预览缓存。请使用可写磁盘或解除写保护后重试。")
        return preview

    def ensure(self, source: Path, ident: str) -> Path:
        try:
            return self.validate(source, ident, create=True)
        except OSError:
            raise HTTPException(422, "无法在原视频目录创建缓存，请检查磁盘空间、写保护和共享目录权限。")

    def remove(self, source: Path, ident: str, saved: str) -> None:
        preview = self.validate(source, ident, saved)
        directory = preview.parent
        if directory.exists():
            checked = checked_tree(directory, directory.parent)
            check_plain_tree(checked)
            # Keep ownership until every cached file has gone. A locked file or
            # disconnected share must not turn a retry into an unowned-folder error.
            marker = checked / MARKER
            for child in checked.iterdir():
                if child == marker:
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            marker.unlink()
            try:
                checked.rmdir()
            except OSError:
                # The final empty-directory removal can fail too (for example,
                # an open directory handle). Restore only our missing marker,
                # never overwrite a marker or follow a replacement link.
                checked_tree(checked, directory.parent)
                if checked.is_dir():
                    try:
                        with marker.open("x", encoding="utf-8") as handle:
                            json.dump(self.owner(ident), handle, ensure_ascii=False)
                    except FileExistsError:
                        pass
                raise

    @staticmethod
    def copy_video_cache(old, new, old_story: Path, new_story: Path) -> None:
        """Publish only complete copied files; preserve the old tree until DB commit."""
        check_plain_tree(old.target.parent)
        check_plain_tree(new.target.parent)
        prefixes = {old.target.stem, old.legacy_preview.stem if old.legacy_preview else old.target.stem}
        for prefix in prefixes:
            for item in old.target.parent.glob(prefix + ".*"):
                if item.is_file() and not item.name.endswith((".partial.mp4", ".partial.jpg", ".migrating")):
                    link_copy(item, new.target.parent / item.name)
        if old_story.is_dir() and old_story != new_story:
            check_plain_tree(old_story)
            for item in old_story.rglob("*"):
                if item.is_file():
                    link_copy(item, new_story / item.relative_to(old_story))
