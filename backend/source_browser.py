"""Read-only browsing of the NAS mount exposed to the intranet container."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

from .service import VIDEO_EXTENSIONS

PAGE_SIZE = 200


def browse_sources(root: Path, raw_path: str | None = None, page: int = 0) -> dict:
    if page < 0:
        raise HTTPException(422, "目录页码无效。")
    try:
        base = root.resolve(strict=True)
        if not base.is_dir():
            raise HTTPException(503, "NAS 素材目录未挂载。")
    except OSError:
        raise HTTPException(503, "NAS 素材目录不可访问。")
    requested = Path(raw_path) if raw_path else base
    if not requested.is_absolute() or ".." in requested.parts:
        raise HTTPException(422, "只能浏览 NAS 挂载目录内的素材。")
    try:
        directory = requested.resolve(strict=True)
        if not directory.is_relative_to(base) or not directory.is_dir():
            raise HTTPException(403, "只能浏览 NAS 挂载目录内的素材。")
        entries = []
        with os.scandir(directory) as listing:
            for item in listing:
                if item.name.startswith(".") or item.name in {"timeline", "__pycache__"} or item.is_symlink():
                    continue
                if item.is_dir(follow_symlinks=False):
                    entries.append({"name": item.name, "path": str(directory / item.name), "kind": "directory"})
                elif item.is_file(follow_symlinks=False) and Path(item.name).suffix.casefold() in VIDEO_EXTENSIONS:
                    entries.append({"name": item.name, "path": str(directory / item.name), "kind": "file", "size": item.stat(follow_symlinks=False).st_size})
    except OSError:
        raise HTTPException(503, "无法读取 NAS 目录，请检查挂载和读取权限。")
    entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["name"]))
    start = page * PAGE_SIZE
    return {"root": str(base), "path": str(directory), "parent": str(directory.parent) if directory != base else None,
            "entries": entries[start:start + PAGE_SIZE], "page": page, "has_more": len(entries) > start + PAGE_SIZE}
