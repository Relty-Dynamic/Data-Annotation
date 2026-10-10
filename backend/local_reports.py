"""Private central summaries for projects whose original media remains on an annotator's computer."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from .service import AXES, FILENAMES, DEFAULT_TRACK_LABELS, validate_annotations, validate_custom_tracks, validate_fixed_tracks, validate_track_labels

PROJECT_ID = re.compile(r"[0-9a-f]{32}\Z")


def _file_name(axis: str) -> str:
    return FILENAMES.get(axis, axis + ".timeline.json")


def _safe_folder(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")[:48] or "未命名项目"


def validate_report(snapshot: dict, documents: dict[str, str] | None) -> dict:
    if not isinstance(snapshot, dict) or not PROJECT_ID.fullmatch(str(snapshot.get("id", ""))):
        raise HTTPException(422, "本机项目编号无效。")
    name = snapshot.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise HTTPException(422, "本机项目名称无效。")
    source_folder = snapshot.get("source_folder_name", name)
    if (not isinstance(source_folder, str) or not source_folder.strip() or len(source_folder) > 255
            or source_folder in (".", "..") or "/" in source_folder or "\\" in source_folder):
        raise HTTPException(422, "本机采集文件夹名称无效。")
    revision = snapshot.get("revision")
    duration = snapshot.get("duration_ms")
    if type(revision) is not int or revision < 0 or type(duration) is not int or duration <= 0:
        raise HTTPException(422, "本机项目版本或时长无效。")
    videos = snapshot.get("videos")
    if not isinstance(videos, list) or not 1 <= len(videos) <= 1000:
        raise HTTPException(422, "本机项目视频列表无效。")
    for video in videos:
        if (not isinstance(video, dict) or not isinstance(video.get("name"), str)
                or type(video.get("start_ms")) is not int or type(video.get("end_ms")) is not int
                or video["start_ms"] < 0 or video["end_ms"] > duration or video["start_ms"] >= video["end_ms"]):
            raise HTTPException(422, "本机视频时间范围无效。")
    tracks = validate_custom_tracks(snapshot.get("custom_tracks", []))
    fixed = validate_fixed_tracks(snapshot.get("fixed_tracks", list(AXES)))
    labels = validate_track_labels(snapshot.get("track_labels", DEFAULT_TRACK_LABELS))
    annotations = validate_annotations(snapshot.get("annotations"), duration, videos=videos,
                                       custom_tracks=tracks, enabled_fixed_tracks=fixed, fixed_label_options=labels)
    safe = {"id": snapshot["id"], "name": name.strip(), "source_folder_name": source_folder.strip(),
            "revision": revision,
            "duration_ms": duration, "videos": [{key: video.get(key) for key in ("id", "name", "start_ms", "end_ms", "duration_ms", "recording_start")}
                                                 for video in videos], "fixed_tracks": fixed, "track_labels": labels, "custom_tracks": tracks, "annotations": annotations,
            "submitted_at": None}
    if documents is not None:
        expected = set(AXES) | {item["id"] for item in tracks}
        if (not isinstance(documents, dict) or set(documents) != expected
                or any(not isinstance(value, str) for value in documents.values())
                or sum(len(value) for value in documents.values()) > 20_000_000):
            raise HTTPException(422, "本机时间轴文件不齐全或过大。")
        save_ids, timebases, saved_ats = set(), [], []
        for axis, value in documents.items():
            try:
                item = json.loads(value)
                timebase = item["timebase"]
                if (item["schema_version"] != 3 or item["axis"] != axis
                        or item["collection_id"] != safe["id"] or item["collection_name"] != safe["name"]
                        or timebase["duration_ms"] != duration
                        or timebase.get("fixed_tracks", list(AXES)) != fixed
                        or timebase.get("track_labels", DEFAULT_TRACK_LABELS) != labels
                        or timebase.get("custom_tracks", []) != tracks):
                    raise ValueError("mismatch")
                exported = [
                    {key: segment[key] for key in record}
                    for segment, record in zip(item["segments"], annotations[axis])
                ]
                if len(item["segments"]) != len(annotations[axis]) or exported != annotations[axis]:
                    raise ValueError("segments mismatch")
                save_ids.add(item["save_id"])
                saved_ats.append(item["saved_at"])
                timebases.append(timebase)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise HTTPException(422, "本机时间轴文件格式或项目编号不一致。")
        if (len(save_ids) != 1 or not next(iter(save_ids))
                or any(item != timebases[0] for item in timebases[1:])
                or any(not isinstance(item, str) or not item for item in saved_ats)):
            raise HTTPException(422, "本机时间轴文件不是同一写回批次。")
        safe["submitted_at"] = saved_ats[0]
    return safe


class LocalReportStore:
    def __init__(self, auth, nas_root: Path | None = None):
        self.auth = auth
        self.nas_root = nas_root
        self.lock = threading.RLock()
        with auth.connection() as con:
            con.execute("CREATE TABLE IF NOT EXISTS local_reports (project_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL, documents TEXT, updated_at TEXT NOT NULL, nas_relative_path TEXT)")
            if "nas_relative_path" not in {row[1] for row in con.execute("PRAGMA table_info(local_reports)")}:
                con.execute("ALTER TABLE local_reports ADD COLUMN nas_relative_path TEXT")

    def _timeline_directory(self, user: dict, safe: dict, previous: str | None) -> tuple[Path, str]:
        if self.nas_root is None:
            raise HTTPException(503, "Ubuntu 尚未配置 NAS 标注输出目录。")
        try:
            root = self.nas_root.resolve(strict=True)
            if not root.is_dir():
                raise OSError("not a directory")
            relative = (Path(previous) if previous else Path("habit") / "smoking"
                        / f'{_safe_folder(safe["source_folder_name"])}-{safe["id"]}' / "timeline")
            if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 4
                    or relative.parts[:2] != ("habit", "smoking") or relative.parts[-1] != "timeline"
                    or not relative.parts[2].endswith("-" + safe["id"])):
                raise HTTPException(409, "本机项目的 NAS 目录记录无效，已停止写回。")
            folder = root
            for part in relative.parts[:2]:
                folder = folder / part
                if folder.is_symlink() or not folder.is_dir():
                    raise HTTPException(503, "NAS 的 habit/smoking 采集目录不可用，未写回。")
            for part in relative.parts[2:]:
                folder = folder / part
                if folder.is_symlink():
                    raise HTTPException(409, "NAS 标注目录包含链接，已停止写回。")
                folder.mkdir(exist_ok=True)
                if not folder.is_dir():
                    raise OSError("not a directory")
            return folder, str(relative)
        except OSError as error:
            raise HTTPException(503, "无法在 NAS 建立本机项目目录，请检查挂载及写入权限。") from error

    @contextmanager
    def _write_nas(self, user: dict, safe: dict, documents: dict[str, str],
                   old_documents: dict[str, str] | None, previous: str | None):
        folder, relative = self._timeline_directory(user, safe, previous)
        desired = {_file_name(axis): value.encode("utf-8") for axis, value in documents.items()}
        old = {_file_name(axis): value.encode("utf-8") for axis, value in (old_documents or {}).items()}
        names = set(desired) | set(old)
        touched: list[str] = []
        temporaries: list[Path] = []
        backup_parent = folder.parent / ".annotation-backups"
        backup_dir = backup_parent / uuid.uuid4().hex
        try:
            if backup_parent.is_symlink() or (backup_parent.exists() and not backup_parent.is_dir()):
                raise HTTPException(409, "NAS 备份目录不是普通文件夹，未写回。")
            for path in folder.glob("*.timeline.json"):
                if path.name not in names:
                    raise HTTPException(409, "NAS 目标目录含有未知时间轴文件，未覆盖。")
            for name in names:
                target = folder / name
                if target.is_symlink() or (target.exists() and not target.is_file()):
                    raise HTTPException(409, "NAS 时间轴目标不是普通文件，未覆盖。")
                if target.exists():
                    current = target.read_bytes()
                    if name not in old or current != old[name]:
                        raise HTTPException(409, "NAS 时间轴文件已有外部修改，未覆盖。")
                elif previous and name in old:
                    raise HTTPException(409, "NAS 已写回的时间轴文件缺失，未覆盖。")
            changes = [name for name in names if (folder / name).exists() is False or desired.get(name) != old.get(name)]
            if any((folder / name).exists() for name in changes):
                backup_dir.mkdir(parents=True, exist_ok=False)
                for name in changes:
                    target = folder / name
                    if target.exists():
                        shutil.copy2(target, backup_dir / name)
            for name in changes:
                if name not in desired:
                    continue
                target = folder / name
                temporary = folder / f".{name}.{uuid.uuid4().hex}.tmp"
                with temporary.open("xb") as handle:
                    handle.write(desired[name])
                    handle.flush()
                    os.fsync(handle.fileno())
                temporaries.append(temporary)
            for name in changes:
                target = folder / name
                if target.is_symlink() or (target.exists() and (name not in old or target.read_bytes() != old[name])):
                    raise HTTPException(409, "准备写回时 NAS 时间轴文件发生变化，未覆盖。")
                if not target.exists() and name in old and previous:
                    raise HTTPException(409, "准备写回时 NAS 时间轴文件缺失，未覆盖。")
                if name in desired:
                    temporary = next(path for path in temporaries if path.name.startswith("." + name + "."))
                    os.replace(temporary, target)
                else:
                    target.unlink()
                touched.append(name)
            if (any((folder / name).read_bytes() != content for name, content in desired.items())
                    or any((folder / name).exists() for name in set(old) - set(desired))):
                raise OSError("NAS readback mismatch")
            yield relative
        except BaseException as error:
            failed = []
            for name in reversed(touched):
                target = folder / name
                try:
                    # Never replace a concurrent external edit during recovery.
                    if target.exists() and (name not in desired or target.read_bytes() != desired[name]):
                        failed.append(name)
                        continue
                    backup = backup_dir / name
                    if backup.exists():
                        os.replace(backup, target)
                    else:
                        target.unlink(missing_ok=True)
                except OSError:
                    failed.append(name)
            if failed:
                raise HTTPException(500, "NAS 写回中断且部分文件未能恢复：" + "、".join(failed)) from error
            if isinstance(error, (HTTPException, OSError)):
                if isinstance(error, HTTPException):
                    raise
                raise HTTPException(503, "NAS 写回或读回失败，已尝试恢复旧文件；本机结果保留，可重试。") from error
            raise
        finally:
            for temporary in temporaries:
                temporary.unlink(missing_ok=True)

    def put(self, user: dict, snapshot: dict, documents: dict[str, str] | None) -> dict:
        safe = validate_report(snapshot, documents)
        with self.lock, self.auth.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT owner_id,revision,snapshot,documents,nas_relative_path FROM local_reports WHERE project_id=?", (safe["id"],)).fetchone()
            if old and old[0] != user["id"]:
                raise HTTPException(403, "此本机项目属于其他标注员。")
            if old and safe["revision"] < old[1]:
                raise HTTPException(409, "中央记录已有更新版本，未覆盖。")
            old_snapshot = json.loads(old[2]) if old else None
            if old_snapshot and safe["revision"] == old[1]:
                comparable = {key: value for key, value in old_snapshot.items() if key != "submitted_at"}
                incoming = {key: value for key, value in safe.items() if key != "submitted_at"}
                if comparable != incoming:
                    raise HTTPException(409, "同一项目版本的内容已变化，未覆盖中央记录。")
            old_documents = json.loads(old[3]) if old and old[3] else None
            if documents is None and old and old[1] == safe["revision"] and old_documents:
                safe["submitted_at"] = old_snapshot.get("submitted_at")
            serialized = json.dumps(safe, ensure_ascii=False)
            if len(serialized) > 20_000_000:
                raise HTTPException(422, "本机项目摘要过大。")
            stored_documents = documents or old_documents
            updated = datetime.now().astimezone().isoformat()
            relative = old[4] if old else None
            def save() -> None:
                con.execute("INSERT INTO local_reports VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,snapshot=excluded.snapshot,documents=excluded.documents,updated_at=excluded.updated_at,nas_relative_path=excluded.nas_relative_path",
                            (safe["id"], user["id"], safe["revision"], serialized,
                             json.dumps(stored_documents, ensure_ascii=False) if stored_documents is not None else None,
                             updated, relative))
                con.commit()
            if documents is not None:
                with self._write_nas(user, safe, documents, old_documents, relative) as written_relative:
                    relative = written_relative
                    save()
            else:
                save()
        return {"id": safe["id"], "revision": safe["revision"], "updated_at": updated,
                "has_documents": bool(relative and safe["submitted_at"]), "nas_relative_path": relative}

    def list(self, user: dict) -> list[dict]:
        with self.auth.connection() as con:
            rows = con.execute("SELECT r.owner_id,r.snapshot,r.documents,r.updated_at,u.display_name,r.nas_relative_path FROM local_reports r JOIN users u ON u.id=r.owner_id ORDER BY r.updated_at DESC").fetchall()
        return [{**json.loads(row[1]), "owner_id": row[0], "owner_name": row[4],
                 "has_documents": bool(row[2] and row[5] and json.loads(row[1]).get("submitted_at")),
                 "nas_relative_path": row[5], "updated_at": row[3]}
                for row in rows if user["role"] == "admin" or row[0] == user["id"]]

    def documents(self, user: dict, project_id: str) -> bytes:
        with self.auth.connection() as con:
            row = con.execute("SELECT owner_id,snapshot,documents,nas_relative_path FROM local_reports WHERE project_id=?", (project_id,)).fetchone()
        if not row or (user["role"] != "admin" and row[0] != user["id"]):
            raise HTTPException(404, "找不到这个本机项目。")
        if not row[2] or not row[3] or not json.loads(row[1]).get("submitted_at"):
            raise HTTPException(409, "此本机项目尚未写回 NAS 最终时间轴文件。")
        if self.nas_root is None:
            raise HTTPException(503, "Ubuntu 尚未配置 NAS 标注输出目录。")
        documents = json.loads(row[2])
        relative = Path(row[3])
        if (relative.is_absolute() or len(relative.parts) != 4 or ".." in relative.parts
                or relative.parts[:2] != ("habit", "smoking") or relative.parts[-1] != "timeline"
                or not relative.parts[2].endswith("-" + project_id)):
            raise HTTPException(409, "NAS 目录记录无效，暂不能下载。")
        try:
            root = self.nas_root.resolve(strict=True)
            folder = root
            for part in relative.parts:
                folder = folder / part
                if folder.is_symlink():
                    raise HTTPException(409, "NAS 目录含有链接，暂不能下载。")
            if not folder.resolve(strict=True).is_relative_to(root):
                raise HTTPException(409, "NAS 目录超出采集根目录，暂不能下载。")
        except OSError as error:
            raise HTTPException(503, "无法访问 NAS 时间轴目录，请检查挂载。") from error
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for axis, value in documents.items():
                path = folder / _file_name(axis)
                try:
                    if path.is_symlink() or path.read_bytes() != value.encode("utf-8"):
                        raise HTTPException(409, "NAS 时间轴文件已变化，暂不能下载，请核对。")
                except OSError as error:
                    raise HTTPException(503, "无法读回 NAS 时间轴文件，请检查挂载。") from error
                archive.writestr(_file_name(axis), value)
        return output.getvalue()
