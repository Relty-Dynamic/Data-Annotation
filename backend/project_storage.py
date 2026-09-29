from __future__ import annotations

import os
import filecmp
import re
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import HTTPException

LOCAL_TIME = timezone(timedelta(hours=8))
PROJECT_ID = re.compile(r"[0-9a-f]{32}\Z")


def import_name(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(LOCAL_TIME).strftime("%m%d-%H-%M")


def checked_tree(path: Path, parent: Path) -> Path:
    """Deletion/migration only operates on a direct managed child, without reparse points."""
    parent = parent.resolve()
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise HTTPException(409, "项目目录包含外部链接，已停止清理。")
    resolved = path.resolve()
    if resolved.parent != parent:
        raise HTTPException(409, "项目目录超出本地存储范围，已停止清理。")
    if resolved.exists():
        for current, dirs, files in os.walk(resolved, followlinks=False):
            for name in dirs + files:
                item = Path(current) / name
                if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                    raise HTTPException(409, "项目目录包含外部链接，已停止清理。")
    return resolved


def link_copy(source: Path, target: Path):
    if target.exists():
        if not os.path.samefile(source, target) and not filecmp.cmp(source, target, shallow=False):
            raise HTTPException(409, "缓存迁移目标与已有文件不一致，已保留双方文件。")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        temporary = target.with_name(target.name + ".migrating")
        shutil.copy2(source, temporary)
        if not filecmp.cmp(source, temporary, shallow=False):
            raise HTTPException(409, "缓存复制后内容校验失败，原缓存仍保留，请检查目标磁盘后重试。")
        os.replace(temporary, target)


class ProjectStorage:
    def __init__(self, local: Path):
        self.root = local / "projects"
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, ident: str) -> Path:
        if not isinstance(ident, str) or not PROJECT_ID.fullmatch(ident):
            raise HTTPException(404, "找不到这个标注项目。")
        path = self.root / ident
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or path.resolve().parent != self.root.resolve():
            raise HTTPException(409, "项目目录超出本地存储范围。")
        return path

    def infer_import_time(self, project: dict, old_imports: Path, candidates: list[Path]) -> tuple[str, bool]:
        if project.get("imported_at"):
            return project["imported_at"], bool(project.get("import_time_estimated"))
        if old_imports.is_dir():
            return datetime.fromtimestamp(old_imports.stat().st_ctime, timezone.utc).isoformat(), True
        times = [p.stat().st_ctime for p in candidates if p.exists()]
        fallback = datetime.fromisoformat(project["updated_at"])
        return (datetime.fromtimestamp(min(times), timezone.utc).isoformat() if times else fallback.isoformat()), True

    def remove(self, ident: str):
        directory = checked_tree(self.directory(ident), self.root)
        if directory.exists():
            shutil.rmtree(directory)
