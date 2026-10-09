from __future__ import annotations

import hashlib
import copy
import io
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .previews import PreviewManager, PreviewSpec, run_ffmpeg
from .project_storage import ProjectStorage, import_name, checked_tree, link_copy
from .source_cache import SourceCache, CACHE_FOLDER, check_plain_tree
from .session_cache import SessionCache
from .remote import RemoteClient
from .storyboards import StoryboardCache
from .video_selection import active_videos, skipped_videos

LOGGER = logging.getLogger("uvicorn.error")

LEGACY_AXES = ("scene", "posture", "habit")
AXES = ("scene", "posture", "category", "habit")
COVERAGE_AXES = ("scene", "posture")
AXIS_NAMES = {"scene": "场景", "posture": "姿势", "category": "大类", "habit": "习惯"}
FILENAMES = {axis: f"{axis}.timeline.json" for axis in AXES}


def filename_for_axis(axis: str) -> str:
    if axis in FILENAMES or re.fullmatch(r"custom_[0-9a-f]{32}", axis):
        return f"{axis}.timeline.json"
    raise ValueError("Invalid timeline axis")
FIXED_LABELS = {
    "scene": {"室内": "indoor", "室外": "outdoor", "车内": "in_vehicle", "其他": "other"},
    "posture": {"动": "moving", "坐": "sitting", "站": "standing", "躺": "lying"},
    "category": {"专注": "focus", "活动": "activity", "用餐": "dining", "通勤": "commuting",
                 "社交": "social", "放松": "relaxation", "休息": "rest", "其他": "other"},
}
# Inclusive overlap/offset limits; real nominal gaps keep the stricter seam rule.
MAX_CAMERA_OVERLAP_MS = 3000
MAX_CAMERA_ALIGNMENT_MS = 3000
CAMERA_SEAM_EXCLUSIVE_MS = 1000

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".mts", ".m2ts", ".mpg", ".mpeg", ".wmv"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_project_name(value: Any, *, allow_empty: bool = False) -> str | None:
    """Project names are metadata, never filesystem paths."""
    if value is None and allow_empty:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "项目名称必须为文本。")
    if any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise HTTPException(422, "项目名称不能包含控制字符。")
    name = value.strip()
    if not name:
        if allow_empty:
            return None
        raise HTTPException(422, "项目名称不能为空。")
    if len(name) > 80:
        raise HTTPException(422, "项目名称不能超过 80 个字符。")
    return name


def validate_custom_tracks(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > 16:
        raise HTTPException(422, "自定义时间轴最多 16 条。")
    result, ids = [], set()
    for track in value:
        if not isinstance(track, dict) or set(track) != {"id", "name", "mode", "labels"}:
            raise HTTPException(422, "自定义时间轴格式不正确。")
        ident = track["id"]
        if not isinstance(ident, str) or not re.fullmatch(r"custom_[0-9a-f]{32}", ident) or ident in ids:
            raise HTTPException(422, "自定义时间轴 ID 不正确或重复。")
        ids.add(ident)
        name = validate_project_name(track["name"])
        mode = track["mode"]
        if mode not in ("state", "event"):
            raise HTTPException(422, "自定义时间轴类型不正确。")
        labels = track["labels"]
        if not isinstance(labels, list) or len(labels) > 32 or (mode == "state" and not labels) or (mode == "event" and labels):
            raise HTTPException(422, "状态轴需要标签，事件轴不设置固定标签。")
        cleaned = []
        for label in labels:
            label = validate_project_name(label)
            if label in cleaned:
                raise HTTPException(422, "同一时间轴不能有重复标签。")
            cleaned.append(label)
        result.append({"id": ident, "name": name, "mode": mode, "labels": cleaned})
    return result


def natural_key(value: str) -> list[tuple[int, Any]]:
    return [(1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r"(\d+)", value)]


def parse_recording_start(filename: str) -> datetime:
    """Parse an explicit camera filename timestamp; never infer a missing time."""
    patterns = (
        r"(?<!\d)(?P<year>(?:19|20|21)\d{2})(?P<month>\d{2})(?P<day>\d{2})(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?P<ms>\d{3})?(?!\d)",
        r"(?<!\d)(?P<year>(?:19|20|21)\d{2})(?P<month>\d{2})(?P<day>\d{2})[_ T-](?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?:\.(?P<ms>\d{3})(?!\d))?(?!\d)",
        r"(?<!\d)(?P<year>(?:19|20|21)\d{2})-(?P<month>\d{2})-(?P<day>\d{2})[_ T](?P<hour>\d{2})[-:](?P<minute>\d{2})[-:](?P<second>\d{2})(?:\.(?P<ms>\d{3})(?!\d))?(?!\d)",
    )
    candidates = set()
    for pattern in patterns:
        for match in re.finditer(pattern, Path(filename).stem):
            parts = match.groupdict()
            try:
                stamp = datetime(*(int(parts[key]) for key in ("year", "month", "day", "hour", "minute", "second")), microsecond=int(parts.get("ms") or "0") * 1000, tzinfo=timezone(timedelta(hours=8)))
                candidates.add(stamp)
            except ValueError:
                continue
    if len(candidates) != 1:
        detail = "含有多个不同录制时间" if candidates else "无法识别录制开始时间"
        raise HTTPException(422, f"文件名 {filename} {detail}。请使用 YYYYMMDDHHMMSS、YYYYMMDD_HHMMSS 或 YYYY-MM-DD_HH-mm-ss 格式；不允许猜测时间或直接拼接。")
    return next(iter(candidates))


def alignment_warnings(videos: list[dict]) -> list[str]:
    offsets = [video["alignment_offset_ms"] for video in videos if video.get("alignment_offset_ms", 0) > 0]
    if not offsets:
        return []
    return [f"已校正 {len(offsets)} 处秒精度文件名的相邻片段边界，最大累计偏移 {max(offsets)} 毫秒；原始边界重叠不超过 {MAX_CAMERA_OVERLAP_MS} 毫秒，累计偏移不超过 {MAX_CAMERA_ALIGNMENT_MS} 毫秒；名义间隔须小于 {CAMERA_SEAM_EXCLUSIVE_MS} 毫秒。保留全部视频帧，原始文件名时间和校正偏移均已保留用于追溯。"]


def recording_layout(videos: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Derive logical recording runs without moving saved annotation coordinates.

    Camera filenames only contain whole seconds. A subsecond positive seam can
    be that timestamp rounding, just like the already aligned negative seams.
    Apply the same camera/index/error guards instead of a general gap threshold.
    """
    ordered = sorted(videos, key=lambda item: item["start_ms"])
    runs, bridges, gaps = [], [], []
    for index, video in enumerate(ordered):
        start, end = video["start_ms"], video["end_ms"]
        previous = ordered[index - 1] if index else None
        bridge = None
        if previous and 0 < start - previous["end_ms"] < CAMERA_SEAM_EXCLUSIVE_MS:
            left = re.fullmatch(r"([A-Za-z0-9]+)_(\d{14})_(\d+)", Path(previous.get("name", "")).stem)
            right = re.fullmatch(r"([A-Za-z0-9]+)_(\d{14})_(\d+)", Path(video.get("name", "")).stem)
            if (left and right and left.group(1) == right.group(1)
                    and int(right.group(3)) == int(left.group(3)) + 1
                    and right.group(2) > left.group(2)):
                try:
                    nominal_delta = round((parse_recording_start(video["name"]) - parse_recording_start(previous["name"])).total_seconds() * 1000)
                    raw_error = previous["duration_ms"] - nominal_delta
                    offset_bound = max(abs(previous.get("alignment_offset_ms", 0)), abs(video.get("alignment_offset_ms", 0)))
                    if abs(raw_error) < CAMERA_SEAM_EXCLUSIVE_MS and offset_bound <= MAX_CAMERA_ALIGNMENT_MS:
                        bridge = {"start_ms": previous["end_ms"], "end_ms": start,
                                  "previous_video_id": previous.get("id"), "next_video_id": video.get("id"),
                                  "raw_boundary_error_ms": raw_error, "reason": "adjacent_camera_second_precision"}
                except (HTTPException, KeyError, TypeError, ValueError):
                    pass
        if bridge:
            bridges.append(bridge)
        if not runs or (start > runs[-1]["end_ms"] and not bridge):
            if runs:
                gaps.append({"start_ms": runs[-1]["end_ms"], "end_ms": start})
            runs.append({"start_ms": start, "end_ms": end, "video_ids": [video.get("id")]})
        else:
            runs[-1]["end_ms"] = max(runs[-1]["end_ms"], end)
            runs[-1]["video_ids"].append(video.get("id"))
    return runs, bridges, gaps


def selected_recording_layout(project: dict) -> tuple[list[dict], list[dict], list[dict]]:
    runs, bridges, gaps = recording_layout(active_videos(project))
    if project.get('_skipped_video_names') and runs:
        if runs[0]['start_ms'] > 0:
            gaps.insert(0, {'start_ms': 0, 'end_ms': runs[0]['start_ms']})
        if runs[-1]['end_ms'] < project['duration_ms']:
            gaps.append({'start_ms': runs[-1]['end_ms'], 'end_ms': project['duration_ms']})
    return runs, bridges, gaps


def normalize_state_seams(records: list[dict], videos: list[dict], bridges: list[dict]) -> list[dict]:
    """Repair state boundaries created by legacy false gaps, keeping behavior untouched."""
    bridge_ends = {item["start_ms"]: item for item in bridges}
    video_ends = {video.get("id"): video["end_ms"] for video in videos}
    result = []
    for record in records:
        previous = result[-1] if result else None
        bridge = bridge_ends.get(previous["end_ms"]) if previous else None
        same = previous is not None and previous["label"] == record["label"] and all(
            previous.get(key) == record.get(key) for key in ("created_by", "created_at", "updated_by", "updated_at"))
        # Only an old state ending exactly at the seam may resume through that
        # seam. An intentionally unlabelled interval elsewhere remains intact.
        across_seam = bridge and bridge["end_ms"] <= record["start_ms"] < video_ends.get(bridge["next_video_id"], bridge["end_ms"])
        if same and (previous["end_ms"] == record["start_ms"] or across_seam):
            previous["end_ms"] = record["end_ms"]
        else:
            result.append(dict(record))
    return result


def empty_annotations() -> dict:
    return {axis: [] for axis in AXES}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def validate_annotations(value: Any, duration_ms: int, final: bool = False, videos: list | None = None,
                         *, require_coverage: bool = True, require_scene_coverage: bool = True,
                         custom_tracks: list[dict] | None = None) -> dict:
    tracks = validate_custom_tracks(custom_tracks or [])
    custom = {track["id"]: track for track in tracks}
    expected = set(AXES) | set(custom)
    if not isinstance(value, dict) or set(value) != expected and not (not custom and set(value) == set(LEGACY_AXES)):
        raise HTTPException(422, "标注必须包含项目的全部时间轴。")
    # Legacy drafts can be opened without mutating stored annotations/revisions.
    value = {**value, "category": value.get("category", []),
             **{ident: value.get(ident, []) for ident in custom}}
    cleaned = {}
    coverage, bridges, _ = recording_layout(videos) if videos is not None else ([], [], [])
    for axis in (*AXES, *custom):
        if not isinstance(value[axis], list):
            raise HTTPException(422, "时间轴必须为数组。")
        if len(value[axis]) > 100000:
            raise HTTPException(422, "单条时间轴记录过多。")
        records, seen = [], set()
        for record in value[axis]:
            if not isinstance(record, dict):
                raise HTTPException(422, "标注记录格式不正确。")
            ident, label = record.get("id"), record.get("label")
            start, end = record.get("start_ms"), record.get("end_ms")
            kind = record.get("kind", "interval")
            event_axis = axis == "habit" or (axis in custom and custom[axis]["mode"] == "event")
            if kind not in ("point", "interval") or (not event_axis and kind != "interval"):
                raise HTTPException(422, "仅事件轴支持时点标注，标注类型须为 point 或 interval。")
            if not isinstance(ident, str) or not ident or len(ident) > 100 or ident in seen:
                raise HTTPException(422, "标注 ID 为空、重复或过长。")
            seen.add(ident)
            if not isinstance(label, str) or not label.strip() or len(label) > 500:
                raise HTTPException(422, "标注内容不能为空，且不能超过 500 字。")
            label = label.strip()
            if axis in FIXED_LABELS and label not in FIXED_LABELS[axis]:
                raise HTTPException(422, "场景、姿势或大类标签不在允许范围内。")
            if axis in custom and custom[axis]["mode"] == "state" and label not in custom[axis]["labels"]:
                raise HTTPException(422, "自定义状态标签不在该时间轴的选项中。")
            if type(start) is not int or not 0 <= start <= duration_ms:
                raise HTTPException(422, "开始时间须为视频范围内的整数毫秒。")
            if kind == "point":
                if type(end) is not int or end != start:
                    raise HTTPException(422, "时点标注的开始和结束时间必须相同。")
            else:
                if start == duration_ms:
                    raise HTTPException(422, "持续标注必须在视频结束前开始。")
                if end is None:
                    if not event_axis or final:
                        raise HTTPException(422, "请先结束所有正在记录的事件，再导出或写回。")
                elif type(end) is not int or not start < end <= duration_ms:
                    raise HTTPException(422, "持续标注的结束时间必须晚于开始时间，且不能超出视频总时长。")
            if videos is not None:
                if kind == "point":
                    if not any(video["start_ms"] <= start <= video["end_ms"] for video in coverage):
                        raise HTTPException(422, "时点标注位于无视频空档，请选择实际视频中的时刻。")
                elif end is None:
                    if not any(video["start_ms"] <= start < video["end_ms"] for video in coverage):
                        raise HTTPException(422, "持续标注的起点位于无视频空档。")
                else:
                    covered = sum(max(0, min(end, video["end_ms"]) - max(start, video["start_ms"])) for video in coverage)
                    if covered != end - start:
                        raise HTTPException(422, "标注区间不能跨越无视频空档，请分别标注两侧视频。")
            cleaned_record = {"id": ident, "label": label, "kind": kind, "start_ms": start, "end_ms": end}
            for key in ("created_by", "created_by_name", "created_at", "updated_by", "updated_by_name", "updated_at"):
                item = record.get(key)
                if item is not None:
                    if not isinstance(item, str) or len(item) > 120:
                        raise HTTPException(422, "标注人信息格式不正确。")
                    cleaned_record[key] = item
            if axis == "category":
                mode = record.get("mode", "state")
                if mode not in ("state", "overlay"):
                    raise HTTPException(422, "大类标注模式须为 state 或 overlay。")
                cleaned_record["mode"] = mode
            records.append(cleaned_record)
        records.sort(key=lambda item: (item["start_ms"], item["id"]))
        if axis in ("scene", "posture") or (axis in custom and custom[axis]["mode"] == "state"):
            for left, right in zip(records, records[1:]):
                if left["end_ms"] > right["start_ms"]:
                    raise HTTPException(422, "互斥状态轴的同一时间不能存在两条标注。")
        cleaned[axis] = normalize_state_seams(records, videos or [], bridges) if axis in ("scene", "posture") or (axis in custom and custom[axis]["mode"] == "state") else records
    if final and require_coverage:
        runs = coverage if videos is not None else [{"start_ms": 0, "end_ms": duration_ms}]
        for axis in COVERAGE_AXES:
            if axis == "scene" and not require_scene_coverage:
                continue
            records = cleaned[axis]
            index = 0
            for run in runs:
                cursor = run["start_ms"]
                while index < len(records) and records[index]["start_ms"] <= cursor:
                    cursor = max(cursor, records[index]["end_ms"])
                    index += 1
                    if cursor >= run["end_ms"]:
                        break
                if cursor < run["end_ms"]:
                    action = "导出" if axis == "scene" else "导出或写回"
                    raise HTTPException(422, f"{AXIS_NAMES[axis]}轴尚未覆盖全部有视频的时间，请补齐后再{action}。")
    return cleaned


def project_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapped


class ProjectService:
    def __init__(self, root: Path, *, enable_remote: bool = True):
        self.root = root.resolve()
        self.local = self.root / ".local"
        self.imports = self.local / "imports"
        self.cache = self.local / "preview"
        self.preparations = self.local / "preparations"
        for path in (self.local, self.imports, self.cache, self.preparations):
            path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.local / "annotations.sqlite3"
        self.lock = threading.RLock()
        self._clearing_playback: set[str] = set()
        self._playback_generations: dict[str, int] = {}
        self.media_locks: dict[str, threading.Lock] = {}
        self.thumbnail_slots = threading.Semaphore(2)
        self._preparation_guard = threading.Lock()
        self._preparation_scans: dict[str, dict] = {}
        self.storage = ProjectStorage(self.local)
        self.source_cache = SourceCache()
        self.storyboards = StoryboardCache(self.cache, self.storage.root)
        self.previews = PreviewManager(self.render_preview)
        self.sessions = SessionCache(self)
        self.remote = RemoteClient.from_root(self.root) if enable_remote else None
        with self.connection() as con:
            con.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, source_key TEXT, document TEXT NOT NULL, updated_at TEXT NOT NULL)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_projects_source ON projects(source_key)")
            con.execute("CREATE TABLE IF NOT EXISTS edit_events (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision INTEGER NOT NULL, axis TEXT NOT NULL, segment_id TEXT NOT NULL, action TEXT NOT NULL, actor_id TEXT, actor_name TEXT, recorded_at TEXT NOT NULL, before_json TEXT, after_json TEXT)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_edit_events_project ON edit_events(project_id, revision, id)")

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA secure_delete=ON")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def save(self, project: dict) -> None:
        with self.connection() as con:
            con.execute("INSERT INTO projects VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_key=excluded.source_key, document=excluded.document, updated_at=excluded.updated_at", (project["id"], project.get("_source_key"), json.dumps(project, ensure_ascii=False), project["updated_at"]))

    def load(self, ident: str, allow_deleting: bool = False) -> dict:
        with self.connection() as con:
            row = con.execute("SELECT document FROM projects WHERE id=?", (ident,)).fetchone()
        if not row:
            raise HTTPException(404, "找不到这个标注项目。")
        project = json.loads(row[0])
        if project.get("_deleting") and not allow_deleting:
            raise HTTPException(409, "项目正在删除，若清理未完成请重试删除。")
        return project

    def public(self, project: dict) -> dict:
        result = {key: value for key, value in project.items() if not key.startswith("_")}
        result["videos"] = active_videos(project)
        result["skipped_videos"] = skipped_videos(project)
        result["deletion_pending"] = bool(project.get("_deleting"))
        result["playback_generation"] = self._playback_generations.get(project["id"], 0)
        source_count = sum(bool(item.get("cache_directory")) for item in project["_sources"].values())
        result["cache_mode"] = "source" if source_count == len(project["_sources"]) else "mixed" if source_count else "legacy"
        result["cache_directories"] = sorted({item["cache_directory"] for item in project["_sources"].values() if item.get("cache_directory")})
        # External cache metadata is only published after validating the original
        # location. Avoid resolving every NAS path just to display this banner.
        result["needs_source_relink"] = any(not item.get("cache_directory") and self.is_managed_copy(Path(item["path"]))
                                             for item in project["_sources"].values())
        result["warnings"] = list(dict.fromkeys(project.get("warnings", []) + project.get("_cache_warnings", [])))
        result["recording_runs"], result["continuity_bridges"], result["gaps"] = selected_recording_layout(project)
        # Presentation and export share the same normalization. Reading a legacy
        # draft never rewrites its database record or changes its revision.
        result["custom_tracks"] = validate_custom_tracks(project.get("custom_tracks", []))
        result["annotations"] = validate_annotations(project["annotations"], project["duration_ms"], videos=result["videos"],
                                                      custom_tracks=result["custom_tracks"])
        return result

    @project_locked
    def skip_failed_videos(self, ident: str, video_ids: list[str], expected_revision: int) -> dict:
        project = self.load(ident)
        if project['revision'] != expected_revision or ident in self._clearing_playback:
            raise HTTPException(409, '项目已改变，请重新打开并确认失败片段。')
        failed = self.sessions.failed_for_skip(ident, video_ids)
        reasons = dict(project.get('_skipped_video_names', {}))
        reasons.update({item['name']: item.get('detail') or '素材准备失败' for item in failed})
        candidate = {**project, '_skipped_video_names': reasons}
        try:
            validate_annotations(project['annotations'], project['duration_ms'], videos=active_videos(candidate),
                                 custom_tracks=project.get('custom_tracks', []))
        except HTTPException:
            raise HTTPException(409, '失败区间或其相邻间隙已有标注，不能直接跳过；已有草稿已保留。请先处理相关标注。')
        self.sessions.invalidate(ident)
        candidate.update(revision=project['revision'] + 1, updated_at=now(), draft_dirty=True)
        self.save(candidate)
        return self.public(candidate)

    @project_locked
    def restore_skipped_videos(self, ident: str, expected_revision: int) -> dict:
        project = self.load(ident)
        if project['revision'] != expected_revision or ident in self._clearing_playback:
            raise HTTPException(409, '项目已改变，请重新打开后重试。')
        if project.get('_skipped_video_names'):
            self.sessions.invalidate(ident)
            project.pop('_skipped_video_names')
            project.update(revision=project['revision'] + 1, updated_at=now(), draft_dirty=True)
            self.save(project)
        return self.public(project)

    def projects(self) -> list:
        with self.connection() as con:
            rows = con.execute("SELECT document FROM projects ORDER BY updated_at DESC").fetchall()
        return [self.public(json.loads(row[0])) for row in rows]

    def migrate_legacy_projects(self) -> dict:
        """Copy/link all owners first, then remove old managed copies. Annotation coordinates stay unchanged."""
        with self.lock:
            with self.connection() as con:
                projects = [json.loads(row[0]) for row in con.execute("SELECT document FROM projects")]
            remove_files, remove_trees = set(), set()
            migrated = []
            for project in projects:
                if project.get("_deleting") or project.get("_storage_version", 0) >= 3:
                    continue
                ident = project["id"]
                destination = self.storage.directory(ident)
                checked_tree(destination, self.storage.root)
                destination.mkdir(parents=True, exist_ok=True)
                cache = destination / "preview"
                cache.mkdir(exist_ok=True)
                old_imports = self.imports / ident
                checked_tree(old_imports, self.imports)
                candidates = []
                for video in project["videos"]:
                    old = self.preview_spec(project, video["id"], legacy=True)
                    scoped = self.preview_spec(project, video["id"], check_source=False)
                    prefixes = [old.key, project["source_fingerprint"] + "_" + video["id"]]
                    for prefix in prefixes:
                        for path in self.cache.glob(prefix + ".*"):
                            if not path.is_file() or path.is_symlink():
                                continue
                            candidates.append(path)
                            link_copy(path, cache / path.name)
                            remove_files.add(path)
                    old_story = self.cache / "storyboard-s1" / old.key
                    checked_tree(old_story, self.cache / "storyboard-s1")
                    if old_story.is_dir():
                        new_story = self.storyboards.directory(scoped.key)
                        for path in old_story.rglob("*"):
                            if path.is_file():
                                link_copy(path, new_story / path.relative_to(old_story))
                        remove_trees.add(old_story)
                imported_at, estimated = self.storage.infer_import_time(project, old_imports, candidates)
                if old_imports.is_dir():
                    for path in old_imports.rglob("*"):
                        if path.is_file():
                            link_copy(path, destination / "imports" / path.relative_to(old_imports))
                    for source in project["_sources"].values():
                        path = Path(source["path"])
                        if path.is_relative_to(old_imports):
                            source.setdefault("cache_identity_path", source["path"])
                            source["path"] = str(destination / "imports" / path.relative_to(old_imports))
                    remove_trees.add(old_imports)
                old_preparation = self.preparations / (ident + ".json")
                if old_preparation.is_file():
                    link_copy(old_preparation, destination / "preparation.json")
                    remove_files.add(old_preparation)
                if not project.get("_custom_name"):
                    project["name"] = import_name(imported_at)
                project.update(imported_at=imported_at, import_time_estimated=estimated, _storage_version=2)
                self.save(project)
                migrated.append({"id": ident, "name": project["name"], "estimated": estimated})
            # Every live project now references a complete private directory. Hard links
            # shared with another project remain valid when this old directory entry goes.
            for path in remove_files:
                if path.resolve().parent not in {self.cache.resolve(), self.preparations.resolve()} or path.is_symlink():
                    raise HTTPException(409, "旧缓存路径越界，已停止迁移清理。")
                path.unlink(missing_ok=True)
            for path in remove_trees:
                parent = self.imports if path.parent == self.imports else self.cache / "storyboard-s1"
                checked = checked_tree(path, parent)
                if checked.exists():
                    shutil.rmtree(checked)
            # Existing path-based projects can relocate their disposable previews.
            # Uploaded copies remain untouched until an original is fully verified.
            for project in projects:
                if project.get("_deleting"):
                    continue
                current = self.load(project["id"])
                try:
                    self.attach_source_caches(current, migrate=True)
                    self.save(current)
                    self.cleanup_relinked_sources(current)
                except (HTTPException, OSError) as exc:
                    current["_cache_warnings"] = [str(exc.detail) if isinstance(exc, HTTPException) else "原目录缓存尚未迁移，请连接原目录并检查写入权限后重试。"]
                    self.save(current)
            return {"projects": migrated, "migrated": len(migrated)}

    def is_managed_copy(self, path: Path) -> bool:
        resolved = path.resolve()
        return resolved.is_relative_to(self.imports.resolve()) or resolved.is_relative_to(self.storage.root.resolve())

    def attach_source_caches(self, project: dict, *, video_ids: set[str] | None = None, migrate: bool = False) -> None:
        warnings = []
        for video in project["videos"]:
            if video_ids is not None and video["id"] not in video_ids:
                continue
            source = project["_sources"][video["id"]]
            if source.get("cache_directory") or self.is_managed_copy(Path(source["path"])):
                continue
            try:
                path = Path(source["path"])
                self.check_media_source(path, source)
                directory = self.source_cache.ensure(path, project["id"])
                if migrate:
                    LOGGER.info("Relocating preview beside original: %s", video["name"])
                    old = self.preview_spec(project, video["id"], check_source=False)
                    old_story = self.storyboards.directory(old.key)
                    updated = copy.deepcopy(project)
                    updated["_sources"][video["id"]]["cache_directory"] = str(directory)
                    new = self.preview_spec(updated, video["id"], check_source=False)
                    self.source_cache.copy_video_cache(old, new, old_story, self.storyboards.directory(new.key))
                source["cache_directory"] = str(directory)
            except (HTTPException, OSError):
                if not migrate:
                    raise
                warnings.append("视频 " + video["name"] + " 的缓存仍保留在平台本地；请连接可写的原目录后重试。")
        project["_storage_version"] = 3
        project["_cache_warnings"] = warnings
        if project["_sources"] and all(item.get("cache_directory") for item in project["_sources"].values()):
            project["_local_cache_cleanup"] = True

    @staticmethod
    def full_file_hash(path: Path) -> str:
        value = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(8 * 1024 * 1024):
                value.update(block)
        return value.hexdigest()

    def cleanup_relinked_sources(self, project: dict) -> int:
        removed = 0
        pending = []
        referenced = set()
        # Resolving NAS paths can block for minutes when a share is unavailable.
        # This safety check is needed only when a duplicate is actually pending;
        # every pending duplicate still receives the full cross-project check.
        if project.get("_relink_cleanup"):
            with self.connection() as con:
                other_sources = [source for row in con.execute("SELECT document FROM projects WHERE id<>?", (project["id"],))
                                 for source in json.loads(row[0])["_sources"].values()]
            referenced = {os.path.normcase(str(Path(source["path"]).resolve())) for source in other_sources}
        for item in project.get("_relink_cleanup", []):
            old = Path(item["path"])
            try:
                # Every removed original copy must be inside this project's imports.
                imports = self.storage.directory(project["id"]) / "imports"
                check_plain_tree(imports)
                if not old.resolve().is_relative_to(imports.resolve()):
                    raise HTTPException(409, "旧视频副本路径超出项目导入目录，已停止清理。")
                if os.path.normcase(str(old.resolve())) in referenced:
                    raise HTTPException(409, "其他项目仍引用此视频副本，已保留文件。")
                current = project["_sources"][item["video_id"]]
                original = Path(current["path"])
                self.check_media_source(original, current)
                if not old.exists():
                    continue
                if self.full_file_hash(old) != item["sha256"] or self.full_file_hash(original) != item["sha256"]:
                    raise HTTPException(409, "关联后视频内容发生变化，已保留旧视频副本。")
                old.unlink()
                LOGGER.info("Removed verified duplicate: %s", old.name)
                removed += 1
            except (HTTPException, OSError):
                pending.append(item)
        project["_relink_cleanup"] = pending
        if project.get("_local_cache_cleanup") and not pending:
            cache = checked_tree(self.storage.directory(project["id"]) / "preview", self.storage.directory(project["id"]))
            if cache.exists():
                shutil.rmtree(cache)
            project.pop("_local_cache_cleanup", None)
        imports = self.storage.directory(project["id"]) / "imports"
        if imports.is_dir() and not pending:
            check_plain_tree(imports)
            for current, dirs, files in os.walk(imports, topdown=False):
                folder = Path(current)
                if not any(folder.iterdir()):
                    folder.rmdir()
        if pending:
            project["_cache_warnings"] = ["原目录已关联，但部分本地视频副本尚未完成安全清理。请保持原目录在线，再次关联即可继续；标注与视频均已保留。"]
        self.save(project)
        return removed

    @project_locked
    def open_files(self, paths: list[str], name: str | None = None, *, source_writeback: bool = True) -> dict:
        name = validate_project_name(name, allow_empty=True)
        if not paths or len(paths) > 1000:
            raise HTTPException(422, "请选择 1 到 1000 个原视频。")
        resolved, names = [], set()
        try:
            for value in paths:
                path = Path(value).expanduser().resolve(strict=True)
                if not path.is_file() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
                    raise HTTPException(422, "请选择支持的视频文件。")
                if self.is_managed_copy(path) or CACHE_FOLDER in path.parts:
                    raise HTTPException(422, "请选择原目录中的视频，不要选择平台导入副本或预览缓存。")
                if path.name.casefold() in names:
                    raise HTTPException(422, "选择的视频有重名，请先统一命名以明确视频顺序。")
                names.add(path.name.casefold())
                resolved.append(path)
        except OSError:
            raise HTTPException(422, "无法读取所选原视频，请检查磁盘或共享目录。")
        if not source_writeback:
            if self.source_overlap(resolved):
                raise HTTPException(409, "所选 NAS 视频已在平台项目中，请打开已有项目；无法重复创建独立草稿。")
        parents = {path.parent for path in resolved}
        source_dir = next(iter(parents)) if source_writeback and len(parents) == 1 else None
        project = self.create(resolved, "", source_dir)
        if name is not None:
            project.update(name=name, _custom_name=True)
        # File selection may intentionally choose only part of one directory.
        project["_source_key"] = None
        project["_supplemented"] = True
        project["_external_hashes"] = self.current_external_hashes(source_dir) if source_dir else {axis: None for axis in AXES}
        self.attach_source_caches(project)
        self.save(project)
        return self.public(project)

    def source_overlap(self, paths: list[Path], except_id: str | None = None) -> bool:
        selected = {os.path.normcase(str(path)) for path in paths}
        with self.connection() as con:
            existing = [json.loads(row[0]) for row in con.execute("SELECT document FROM projects")]
        return any(project["id"] != except_id and selected.intersection(
            os.path.normcase(source["path"]) for source in project.get("_sources", {}).values())
            for project in existing)

    def relink_sources(self, ident: str, raw_path: str, expected_revision: int) -> dict:
        self.sessions.invalidate(ident, preserve_ready=True)
        if not raw_path or not raw_path.strip().strip('"'):
            raise HTTPException(422, "请输入原视频所在目录。")
        try:
            directory = Path(raw_path.strip().strip('"')).expanduser().resolve(strict=True)
            if not directory.is_dir() or self.is_managed_copy(directory) or CACHE_FOLDER in directory.parts:
                raise HTTPException(422, "请选择平台项目文件夹之外的原视频目录。")
            folders = [directory] + [item for item in directory.iterdir() if item.is_dir() and item.name.casefold() == "fpv"]
            found = {}
            for folder in folders:
                for path in folder.iterdir():
                    if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS:
                        name = path.name.casefold()
                        if name in found:
                            raise HTTPException(422, "原目录与 FPV 子目录含有重名视频，请选择明确的视频目录。")
                        found[name] = path
        except OSError:
            raise HTTPException(422, "无法访问原视频目录，请连接 SD 卡或共享目录后重试。")
        with self.lock:
            current = self.load(ident)
            if current["revision"] != expected_revision:
                raise HTTPException(409, "项目已在其他窗口更新，请重新打开后关联原目录。")
        self.previews.cancel_project(ident)
        try:
            with self.lock:
                previous = self.load(ident)
                if previous["revision"] != expected_revision:
                    raise HTTPException(409, "项目已在其他窗口更新，请重新打开后关联原目录。")
                if self.source_overlap(list(found.values()), except_id=ident):
                    raise HTTPException(409, "原目录视频已属于其他项目，不能重复关联。")
                project = copy.deepcopy(previous)
                cleanups = list(project.get("_relink_cleanup", []))
                relinked = 0
                mappings = []
                # Verify every byte of every corresponding file before moving any cache.
                for number, video in enumerate(project["videos"], 1):
                    LOGGER.info("Verifying original %s/%s: %s", number, len(project["videos"]), video["name"])
                    source = project["_sources"][video["id"]]
                    original = found.get(video["name"].casefold())
                    if original is None:
                        raise HTTPException(422, "原目录缺少视频：" + video["name"] + "；未修改项目。")
                    old_path = Path(source["path"])
                    self.check_media_source(old_path, source)
                    stamp = self.source_stamp(original)
                    if stamp["size"] != source["stamp"]["size"]:
                        raise HTTPException(409, "同名原视频大小不一致：" + video["name"] + "；已保留本地副本。")
                    checksum = self.full_file_hash(original)
                    if self.full_file_hash(old_path) != checksum:
                        raise HTTPException(409, "同名原视频内容不一致：" + video["name"] + "；已保留本地副本。")
                    if self.source_stamp(original) != stamp:
                        raise HTTPException(409, "校验期间原视频发生变化，未修改项目。")
                    self.check_media_source(old_path, source)
                    mappings.append((video, old_path, original, stamp, checksum))
                for video, old_path, original, stamp, checksum in mappings:
                    LOGGER.info("Migrating preview cache: %s", video["name"])
                    source = project["_sources"][video["id"]]
                    old = self.preview_spec(previous, video["id"], check_source=False)
                    old_story = self.storyboards.directory(old.key)
                    destination = self.source_cache.ensure(original, ident)
                    source.setdefault("cache_identity_path", source["path"])
                    source.setdefault("cache_identity_stamp", copy.deepcopy(source["stamp"]))
                    source.update(path=str(original), stamp=stamp, cache_directory=str(destination))
                    new = self.preview_spec(project, video["id"], check_source=False)
                    self.source_cache.copy_video_cache(old, new, old_story, self.storyboards.directory(new.key))
                    if old_path != original:
                        relinked += 1
                        owned_imports = self.storage.directory(ident) / "imports"
                        if old_path.resolve().is_relative_to(owned_imports.resolve()) and not any(item["path"] == str(old_path) for item in cleanups):
                            cleanups.append({"path": str(old_path), "video_id": video["id"], "sha256": checksum})
                source_dir = directory.parent if directory.name.casefold() == "fpv" else directory
                project.update(source_dir=str(source_dir), _source_key=os.path.normcase(str(source_dir)),
                               _storage_version=3, _supplemented=True, _relink_cleanup=cleanups,
                               _local_cache_cleanup=True, _cache_warnings=[])
                project["_external_hashes"] = self.current_external_hashes(source_dir)
                # IDs, saved annotations, timeline coordinates and revision are unchanged.
                self.save(project)
                try:
                    removed = self.cleanup_relinked_sources(project)
                except (OSError, HTTPException):
                    removed = 0
                    project["_cache_warnings"] = ["原目录关联已保存；部分旧缓存尚未清理，将在下次启动或关联时继续。"]
                    self.save(project)
                public = self.public(project)
                return {"project": public, "relinked_count": relinked, "removed_copies": removed,
                        "warnings": public.get("warnings", [])}
        finally:
            with self.previews.condition:
                self.previews.blocked_projects.discard(ident)

    def delete_project(self, ident: str, expected_revision: int) -> dict:
        raise HTTPException(409, "永久删除已停用。请刷新页面后使用清理本机预览；项目、草稿和 NAS 文件均保留。")

    def start_session(self, ident: str, *, retry_failed: bool = False,
                      cache_generation: int | None = None) -> dict:
        with self.lock:
            generation = self._playback_generations.get(ident, 0)
            if ident in self._clearing_playback or (cache_generation or 0) != generation:
                raise HTTPException(409, "本机预览已清理或正在清理，请重新选择项目后再准备。")
            return self.sessions.start(ident, retry_failed=retry_failed)

    def clear_local_previews(self, ident: str, expected_revision: int) -> dict:
        def playback_directory() -> Path:
            for parent in (self.local, self.storage.root):
                if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                    raise HTTPException(409, "本机缓存目录包含外部链接，已停止清理。")
            directory = self.storage.directory(ident)
            return checked_tree(directory / "playback", directory)

        with self.lock:
            project = self.load(ident, allow_deleting=True)
            if project["revision"] != expected_revision:
                raise HTTPException(409, "该项目标注已在其他窗口更新，请重新确认后清理。")
            directory = playback_directory()
            with self.connection() as con:
                documents = [json.loads(row[0]) for row in con.execute("SELECT document FROM projects")]
            # Lexical normalization does not contact an offline NAS. A source
            # explicitly placed inside playback is not disposable preview data.
            if any(Path(os.path.abspath(item["path"])).is_relative_to(directory)
                   for document in documents for item in document.get("_sources", {}).values()):
                raise HTTPException(409, "有原视频引用本机播放缓存目录，已停止清理，请先调整素材位置。")
            with self.sessions._condition:
                if ident in self._clearing_playback:
                    raise HTTPException(409, "正在清理本机预览，请稍后重试。")
                self._clearing_playback.add(ident)
                self._playback_generations[ident] = self._playback_generations.get(ident, 0) + 1
        try:
            # Stop local writers before removing only their managed playback tree.
            self.sessions.invalidate(ident)
            directory = playback_directory()
            if directory.exists():
                shutil.rmtree(directory)
        except OSError as exc:
            raise HTTPException(409, "部分本机预览仍被占用，请关闭播放窗口后重试清理；项目、草稿和 NAS 文件均保留。") from exc
        finally:
            with self.sessions._condition:
                self._clearing_playback.discard(ident)
        return {"cleared": ident, "playback_generation": self._playback_generations[ident]}

    def clear_submitted_server_cache(self, ident: str, expected_revision: int, save_id: str) -> dict:
        with self.lock:
            project = self.load(ident)
            if (project["revision"] != expected_revision or project.get("draft_dirty")
                    or (project.get("last_writeback") or {}).get("save_id") != save_id):
                raise HTTPException(409, "项目在提交后已有变化，未清理 Ubuntu 播放缓存。")
            return self.clear_local_previews(ident, expected_revision)

    def tool(self, name: str) -> Path | None:
        explicit = os.getenv(name.upper() + "_PATH")
        if explicit and Path(explicit).is_file():
            return Path(explicit)
        if name == "ffprobe" and os.getenv("FFMPEG_PATH"):
            sibling = Path(os.environ["FFMPEG_PATH"]).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
            if sibling.is_file():
                return sibling
        suffix = ".exe" if os.name == "nt" else ""
        preferred = self.root / ".tools" / "ffmpeg" / "bin" / (name + suffix)
        if preferred.is_file():
            return preferred
        tools = self.root / ".tools"
        if tools.exists():
            match = next(tools.rglob(name + suffix), None)
            if match and match.is_file():
                return match
        return None

    def command(self, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "视频处理超时；请检查素材和共享网络后重试。")
        except OSError as exc:
            raise HTTPException(503, f"无法启动视频处理工具：{exc.strerror or type(exc).__name__}")
        if result.returncode:
            raise HTTPException(422, "视频处理失败。素材可能损坏、网络不可用或格式不受支持。")
        return result

    def probe(self, path: Path) -> dict:
        if self.remote and self.remote.map_path(path) is not None:
            return self.remote.inspect(path)['media']
        tool = self.tool("ffprobe")
        if not tool:
            raise HTTPException(503, "缺少 FFprobe。请先运行项目安装脚本。")
        result = self.command([str(tool), "-v", "error", "-show_entries", "format=duration,start_time:stream=codec_type,codec_name,duration,start_time,pix_fmt", "-of", "json", str(path)])
        try:
            info = json.loads(result.stdout)
            streams = info.get("streams", [])
            stream = next(item for item in streams if item.get("codec_type") == "video")
            duration = 0.0
            for candidate in (stream.get("duration"), info.get("format", {}).get("duration")):
                try:
                    parsed = float(candidate)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(parsed) and parsed > 0:
                    duration = parsed
                    break
            if duration <= 0:
                raise ValueError()
            def timestamp(value):
                try:
                    number = float(value)
                    return number if math.isfinite(number) else 0.0
                except (TypeError, ValueError):
                    return 0.0
            media_start = timestamp(stream.get("start_time"))
            format_start = timestamp(info.get("format", {}).get("start_time"))
            return {"duration_ms": max(1, round(duration * 1000)), "media_start_seconds": media_start, "format_start_seconds": format_start, "codec": stream.get("codec_name"), "pixel_format": stream.get("pix_fmt"), "audio_codecs": [item.get("codec_name") for item in streams if item.get("codec_type") == "audio"]}
        except (ValueError, TypeError, KeyError, StopIteration):
            raise HTTPException(422, f"无法识别视频时长：{path.name}")

    def source_stamp(self, path: Path) -> dict:
        if self.remote and self.remote.map_path(path) is not None:
            stamp = self.remote.inspect(path)['stamp']
            try:
                stat = path.stat()
            except OSError:
                # The server still verifies source bytes when the Windows share
                # is unavailable; never replace that check with a local hash read.
                return stamp
            if stat.st_size != stamp['size'] or abs(stat.st_mtime_ns - stamp['mtime_ns']) >= 1_000_000:
                raise HTTPException(409, "本机与服务器读取到的视频身份不一致，请检查素材目录映射或稍后重试。")
            # Preserve fingerprints/cache identities recorded through Windows SMB.
            # Linux and Windows can expose different submillisecond precision.
            return {**stamp, 'mtime_ns': stat.st_mtime_ns}
        try:
            stat = path.stat()
            with path.open("rb") as handle:
                head = handle.read(65536)
                if stat.st_size > 65536:
                    handle.seek(max(65536, stat.st_size - 65536))
                    head += handle.read(65536)
            return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sample_sha256": digest(head)}
        except OSError:
            raise HTTPException(422, f"无法读取视频：{path.name}，请检查路径和共享目录权限。")

    def create(self, paths: list[Path], name: str, source_dir: Path | None, ident: str | None = None, imported_at: str | None = None, *, existing: dict | None = None) -> dict:
        imported_at = imported_at or now()
        if not paths:
            raise HTTPException(422, "目录中没有找到支持的视频。请选择采集根目录、FPV 目录，或直接选择原视频文件。")
        if len(paths) > 1000:
            raise HTTPException(422, "单个项目最多支持 1000 个视频。")
        ident = ident or uuid.uuid4().hex
        ordered = sorted(paths, key=lambda item: (natural_key(item.name), item.name))
        starts = [parse_recording_start(path.name) for path in ordered]
        if starts != sorted(starts):
            raise HTTPException(422, "文件名自然排序与录制时间顺序不一致。请统一视频文件名前缀，确保文件名递增对应录制时间递增。")
        origin = min(starts)
        videos, sources, gaps, cursor = [], {}, [], 0
        existing_by_name = {video["name"].casefold(): video for video in existing["videos"]} if existing else {}
        used_ids = {video["id"] for video in existing_by_name.values()}
        next_id = 1
        for index, path in enumerate(ordered):
            old_video = existing_by_name.get(path.name.casefold())
            if old_video:
                video_id = old_video["id"]
                metadata = existing["_sources"][video_id]
                stamp = metadata["stamp"]
                relative = old_video["relative_path"]
            else:
                metadata = self.probe(path)
                stamp = self.source_stamp(path)
                while f"v{next_id:04d}" in used_ids:
                    next_id += 1
                video_id = f"v{next_id:04d}"
                used_ids.add(video_id)
                relative = str(path.relative_to(source_dir)).replace("\\", "/") if source_dir and path.is_relative_to(source_dir) else path.name
            filename_start_ms = round((starts[index] - origin).total_seconds() * 1000)
            start_ms = filename_start_ms
            alignment_offset_ms = 0
            if start_ms < cursor:
                overlap = cursor - start_ms
                current_camera = re.fullmatch(r"([A-Za-z0-9]+)_(\d{14})_(\d+)", path.stem)
                previous_camera = re.fullmatch(r"([A-Za-z0-9]+)_(\d{14})_(\d+)", ordered[index - 1].stem) if index else None
                adjacent_camera_seconds = (
                    current_camera is not None and previous_camera is not None
                    and current_camera.group(1) == previous_camera.group(1)
                    and int(current_camera.group(3)) == int(previous_camera.group(3)) + 1
                    and starts[index] > starts[index - 1]
                )
                previous_filename_start_ms = round((starts[index - 1] - origin).total_seconds() * 1000) if index else 0
                raw_overlap = previous_filename_start_ms + videos[-1]["duration_ms"] - filename_start_ms if index else overlap
                # Keep original pairwise timing error separate from accumulated
                # alignment. Never consume a nominal gap of one second or more.
                if (not adjacent_camera_seconds
                        or not -CAMERA_SEAM_EXCLUSIVE_MS < raw_overlap <= MAX_CAMERA_OVERLAP_MS
                        or not 0 < overlap <= MAX_CAMERA_ALIGNMENT_MS):
                    raise HTTPException(422, f"视频 {path.name} 的录制时间与上一段重叠 {overlap} 毫秒（原始边界误差 {raw_overlap} 毫秒）。仅同相机、连续序号、秒精度文件名，且原始边界重叠不超过 {MAX_CAMERA_OVERLAP_MS} 毫秒、累计偏移不超过 {MAX_CAMERA_ALIGNMENT_MS} 毫秒、名义间隔不足 {CAMERA_SEAM_EXCLUSIVE_MS} 毫秒时允许校正；请核对素材时间和顺序。")
                alignment_offset_ms = overlap
                start_ms = cursor
            if start_ms > cursor:
                gaps.append({"start_ms": cursor, "end_ms": start_ms})
            cursor = start_ms + metadata["duration_ms"]
            video = {"id": video_id, "name": path.name, "relative_path": relative, "recording_start": starts[index].isoformat(timespec="milliseconds"), "original_media_start_ms": round(metadata.get("media_start_seconds", 0) * 1000), "duration_ms": metadata["duration_ms"], "start_ms": start_ms, "end_ms": cursor, "url": f"/api/media/{ident}/{video_id}", "thumbnail_url": f"/api/thumbnails/{ident}/{video_id}"}
            if alignment_offset_ms:
                video["filename_recording_start"] = video["recording_start"]
                video["filename_start_ms"] = filename_start_ms
                video["alignment_offset_ms"] = alignment_offset_ms
                video["recording_start"] = (starts[index] + timedelta(milliseconds=alignment_offset_ms)).isoformat(timespec="milliseconds")
            videos.append(video)
            sources[video_id] = dict(metadata) if old_video else {"path": str(path.resolve()), "stamp": stamp, **metadata}
        fingerprint = digest(document_bytes([{**video, "id": f"v{index + 1:04d}", "url": None, "thumbnail_url": None, "stamp": sources[video["id"]]["stamp"]} for index, video in enumerate(videos)]))
        project = {"id": ident, "name": import_name(imported_at), "imported_at": imported_at, "import_time_estimated": False, "_storage_version": 2, "source_dir": str(source_dir) if source_dir else None, "duration_ms": cursor, "recording_start": origin.isoformat(timespec="milliseconds"), "gaps": gaps, "videos": videos, "annotations": empty_annotations(), "revision": 0, "updated_at": now(), "source_fingerprint": fingerprint, "draft_dirty": False, "warnings": alignment_warnings(videos), "_sources": sources, "_source_key": os.path.normcase(str(source_dir)) if source_dir else None, "_external_hashes": {axis: None for axis in AXES}}
        return project

    @staticmethod
    def remap_annotations(project: dict, videos: list[dict], duration_ms: int) -> tuple[dict, list[str]]:
        """Keep annotations attached to old media, leaving every added frame unlabelled."""
        old_videos = active_videos(project)
        videos = active_videos({**project, 'videos': videos})
        target = {video["id"]: video for video in videos}
        _, old_bridges, _ = recording_layout(old_videos)
        _, new_bridges, _ = recording_layout(videos)
        bridges = {(item["previous_video_id"], item["next_video_id"]): item for item in new_bridges}
        # Every piece maps old coverage to the same old media in the new layout.
        pieces = [(video["start_ms"], video["end_ms"], target[video["id"]]["start_ms"], target[video["id"]]["end_ms"])
                  for video in old_videos]
        for bridge in old_bridges:
            pair = (bridge["previous_video_id"], bridge["next_video_id"])
            current = bridges.get(pair)
            if current:
                pieces.append((bridge["start_ms"], bridge["end_ms"], current["start_ms"], current["end_ms"]))
            elif target[pair[0]]["end_ms"] == target[pair[1]]["start_ms"]:
                boundary = target[pair[0]]["end_ms"]
                pieces.append((bridge["start_ms"], bridge["end_ms"], boundary, boundary))
        pieces.sort()

        def coordinate(piece, value):
            left, right, new_left, new_right = piece
            return new_left + round((value - left) * (new_right - new_left) / (right - left))

        def point(value):
            # A shared boundary belongs to the video starting there. At a real
            # gap/last frame boundary, preserve the ending video's final frame.
            for video in old_videos:
                if video["start_ms"] <= value < video["end_ms"]:
                    return target[video["id"]]["start_ms"] + value - video["start_ms"]
            for piece in pieces:
                if piece[0] <= value < piece[1]:
                    return coordinate(piece, value)
            for video in reversed(old_videos):
                if value == video["end_ms"]:
                    return target[video["id"]]["end_ms"]
            raise HTTPException(409, "已有标注的时间无法对应原视频，未进行补导入。")

        result = empty_annotations()
        closed_pending = 0
        old_annotations = validate_annotations(project["annotations"], project["duration_ms"], videos=old_videos,
                                               custom_tracks=project.get("custom_tracks", []))
        for axis in old_annotations:
            result.setdefault(axis, [])
            for record in old_annotations[axis]:
                if record["kind"] == "point":
                    mapped = point(record["start_ms"])
                    result[axis].append({**record, "start_ms": mapped, "end_ms": mapped})
                    continue
                stop = project["duration_ms"] if record["end_ms"] is None else record["end_ms"]
                spans = []
                covered = 0
                for piece in pieces:
                    left, right = max(record["start_ms"], piece[0]), min(stop, piece[1])
                    if left >= right:
                        continue
                    covered += right - left
                    mapped_left, mapped_right = coordinate(piece, left), coordinate(piece, right)
                    if mapped_left == mapped_right:
                        continue
                    if spans and spans[-1][1] == mapped_left:
                        spans[-1][1] = mapped_right
                    else:
                        spans.append([mapped_left, mapped_right])
                if not spans or (record["end_ms"] is not None and covered != stop - record["start_ms"]):
                    raise HTTPException(409, "已有标注无法完整对应原视频，未进行补导入，请先检查该标注。")
                if record["end_ms"] is None:
                    if spans[-1][1] == duration_ms:
                        spans[-1][1] = None
                    else:
                        closed_pending += 1
                for index, (start, end) in enumerate(spans):
                    result[axis].append({**record, "id": record["id"] if index == 0 else uuid.uuid4().hex,
                                         "start_ms": start, "end_ms": end})
        warnings = [f"为使新增视频保持未标注，已将 {closed_pending} 条进行中事件结束在原视频末尾，请按需要继续记录。"] if closed_pending else []
        return validate_annotations(result, duration_ms, videos=videos,
                                    custom_tracks=project.get("custom_tracks", [])), warnings

    def supplement(self, ident: str, paths: list[Path], expected_revision: int, skipped_names: list[str] | None = None) -> dict:
        """Atomically extend one project; only incoming sources are probed/read."""
        self.sessions.invalidate(ident, preserve_ready=True)
        with self.lock:
            previous = self.load(ident)
            if previous["revision"] != expected_revision:
                raise HTTPException(409, "该项目已在其他窗口更新，请重新打开项目后补导入。")
            seen = {video["name"].casefold() for video in previous["videos"]}
            skipped, additions = list(skipped_names or []), []
            for path in paths:
                path = path.resolve()
                if self.is_managed_copy(path) or CACHE_FOLDER in path.parts:
                    raise HTTPException(422, "请从原视频目录补导入，不要选择平台导入副本或缓存文件。")
                if path.name.casefold() in seen:
                    if path.name not in skipped:
                        skipped.append(path.name)
                    continue
                if path.suffix.casefold() not in VIDEO_EXTENSIONS:
                    raise HTTPException(422, f"不支持的视频文件格式：{path.name}")
                seen.add(path.name.casefold())
                additions.append(path)
            if not additions:
                return {"project": self.public(previous), "added_count": 0, "skipped_names": skipped}
            if self.source_overlap(additions, except_id=ident):
                raise HTTPException(409, "补导入的视频已属于其他项目，不能由两个标注员同时编辑。")
            if len(previous["videos"]) + len(additions) > 1000:
                raise HTTPException(422, "补导入后单个项目不能超过 1000 个视频。")
            all_paths = [Path(previous["_sources"][video["id"]]["path"]) for video in previous["videos"]] + additions
            fresh = self.create(all_paths, previous["name"], Path(previous["source_dir"]) if previous["source_dir"] else None,
                                ident, imported_at=previous.get("imported_at"), existing=previous)
            annotations, warnings = self.remap_annotations(previous, fresh["videos"], fresh["duration_ms"])
            legacy_fingerprints = dict(previous.get("_legacy_cache_fingerprints", {}))
            for video in previous["videos"]:
                legacy_fingerprints.setdefault(video["id"], previous["source_fingerprint"])
            # Keep source-directory identity and writeback conflict hashes intact.
            # A supplemented project is reopened as this saved composition.
            project = {**previous, **{key: fresh[key] for key in
                       ("videos", "_sources", "source_fingerprint", "duration_ms", "recording_start", "gaps")},
                       "annotations": annotations, "revision": previous["revision"] + 1, "updated_at": now(),
                       "draft_dirty": True, "_supplemented": True, "_supplement_warnings": warnings,
                       "_legacy_cache_fingerprints": legacy_fingerprints,
                       "warnings": alignment_warnings(fresh["videos"]) + warnings}
            self.attach_source_caches(project, video_ids={video["id"] for video in fresh["videos"] if video["name"].casefold() not in {item["name"].casefold() for item in previous["videos"]}})
            public = self.public(project)
            self.save(project)
            return {"project": public, "added_count": len(additions), "skipped_names": skipped}

    def supplement_path(self, ident: str, raw_path: str, expected_revision: int) -> dict:
        if not raw_path or not raw_path.strip().strip('"'):
            raise HTTPException(422, "请输入要补导入的视频文件或目录路径。")
        path = Path(raw_path.strip().strip('"')).expanduser()
        try:
            path = path.resolve(strict=True)
            if path.is_file():
                paths = [path]
            else:
                fpv = next((child for child in path.iterdir() if child.is_dir() and child.name.casefold() == "fpv"), None)
                folder = fpv or path
                paths = [item for item in folder.iterdir() if item.is_file() and item.suffix.casefold() in VIDEO_EXTENSIONS]
        except OSError:
            raise HTTPException(422, "无法访问补导入素材，请检查服务器路径、网络及共享目录权限。")
        if not paths:
            raise HTTPException(422, "没有找到支持的视频，请选择视频文件或所在目录。")
        return self.supplement(ident, paths, expected_revision)

    def reopen_supplemented(self, project: dict, paths: list[Path]) -> dict:
        """A source directory is only one part of a supplemented composition."""
        by_name = {video["name"].casefold(): video for video in project["videos"]}
        for path in paths:
            video = by_name.get(path.name.casefold())
            if video is None:
                raise HTTPException(409, "该目录含有尚未加入项目的视频，请打开现有项目后使用补导入。")
            original = project["_sources"][video["id"]]
            stamp = self.source_stamp(path)
            same_path = os.path.normcase(str(path.resolve())) == os.path.normcase(original["path"])
            if ((same_path and stamp != original["stamp"]) or
                    (stamp["size"], stamp["sample_sha256"]) != (original["stamp"]["size"], original["stamp"]["sample_sha256"])):
                raise HTTPException(409, "该目录中的同名视频内容已改变，本机补导入项目和标注仍保留。")
        hashes = self.current_external_hashes(Path(project["source_dir"]))
        warnings = alignment_warnings(project["videos"]) + project.get("_supplement_warnings", [])
        if hashes != self.expected_external_hashes(project):
            if project.get("draft_dirty"):
                warnings.append("原目录 JSON 已被修改。本机草稿已保留；写回将阻止覆盖外部更改。")
            else:
                imported, hashes, imported_tracks = self.read_external(project, include_tracks=True)
                project["annotations"] = imported or empty_annotations()
                project["custom_tracks"] = imported_tracks
                project["_external_hashes"] = hashes
                project["revision"] += 1
                project["updated_at"] = now()
        project["warnings"] = warnings
        self.save(project)
        return self.public(project)

    def read_external(self, project: dict, *, include_tracks: bool = False) -> tuple:
        source = project.get("source_dir")
        hashes = {axis: None for axis in AXES}
        if not source:
            return (None, hashes, []) if include_tracks else (None, hashes)
        documents = {}
        directory = self.external_directory(Path(source))
        for axis in AXES:
            path = directory / FILENAMES[axis]
            try:
                if path.exists():
                    raw = path.read_bytes()
                    if len(raw) > 30 * 1024 * 1024:
                        raise HTTPException(422, "已有标注 JSON 过大，无法加载。")
                    hashes[axis] = digest(raw)
                    documents[axis] = json.loads(raw.decode("utf-8-sig"))
            except (OSError, ValueError):
                raise HTTPException(422, f"无法读取已有标注文件 {FILENAMES[axis]}。")
        if not documents:
            return (None, hashes, []) if include_tracks else (None, hashes)
        if set(documents) not in (set(LEGACY_AXES), set(AXES)):
            raise HTTPException(409, "原目录标注文件不齐全。请恢复同一保存批次的完整文件后重新打开。")
        tracks = validate_custom_tracks(documents["scene"].get("timebase", {}).get("custom_tracks", []))
        if any(document.get("timebase", {}).get("custom_tracks", []) != tracks for document in documents.values()):
            raise HTTPException(409, "标注文件中的自定义时间轴清单不一致。")
        expected_custom = {track["id"] for track in tracks}
        actual_custom = {path.name.removesuffix(".timeline.json") for path in directory.glob("custom_*.timeline.json")
                         if re.fullmatch(r"custom_[0-9a-f]{32}\.timeline\.json", path.name)}
        if actual_custom != expected_custom:
            raise HTTPException(409, "自定义时间轴文件不齐全或包含其他保存批次。")
        for axis in expected_custom:
            path = directory / filename_for_axis(axis)
            try:
                if path.is_symlink() or not path.is_file():
                    raise HTTPException(409, "自定义时间轴文件不是普通文件。")
                raw = path.read_bytes()
                if len(raw) > 30 * 1024 * 1024:
                    raise HTTPException(422, "已有自定义时间轴 JSON 过大，无法加载。")
                hashes[axis] = digest(raw)
                documents[axis] = json.loads(raw.decode("utf-8-sig"))
            except (OSError, ValueError):
                raise HTTPException(422, f"无法读取已有标注文件 {path.name}。")
        batch_ids, versions = set(), set()
        annotations = empty_annotations()
        annotations.update({axis: [] for axis in expected_custom})
        try:
            selections = [document.get('timebase', {}).get('skipped_videos', []) for document in documents.values()]
            if any(selection != selections[0] for selection in selections):
                raise HTTPException(409, '标注 JSON 中跳过的片段列表不一致，请恢复同一保存批次。')
            selection = selections[0]
            if not isinstance(selection, list):
                raise ValueError()
            names = {}
            for skipped in selection:
                video = next((v for v in project['videos'] if v['name'] == skipped['name']), None)
                if (not video or video['name'] in names or
                        any(skipped[key] != video[key] for key in ('start_ms', 'end_ms')) or
                        not isinstance(skipped.get('reason'), str)):
                    raise ValueError()
                names[video['name']] = skipped['reason']
            selected = {**project, '_skipped_video_names': names}
            if not active_videos(selected):
                raise ValueError()
            for axis, document in documents.items():
                version = document["schema_version"]
                if type(version) is not int or version not in (1, 2, 3) or document["axis"] != axis:
                    raise ValueError()
                versions.add(version)
                fingerprints = [document["timebase"]["source_fingerprint"]]
                aliases = document["timebase"].get("source_fingerprint_aliases", [])
                if isinstance(aliases, list) and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in aliases):
                    fingerprints.extend(aliases)
                if project["source_fingerprint"] not in fingerprints:
                    raise HTTPException(409, "原目录标注对应的素材与当前视频不一致；已停止加载，避免时间轴错配。")
                if document["timebase"]["unit"] != "ms" or document["timebase"]["origin"] != "recording_datetime" or document["timebase"]["recording_start"] != project["recording_start"] or document["timebase"]["duration_ms"] != project["duration_ms"]:
                    raise ValueError()
                if document["timebase"].get("custom_tracks", []) != tracks:
                    raise HTTPException(409, "标注文件中的自定义时间轴清单不一致。")
                if axis in expected_custom:
                    track = next(item for item in tracks if item["id"] == axis)
                    if document.get("axis_name") != track["name"] or document.get("axis_mode") != track["mode"]:
                        raise HTTPException(409, "自定义时间轴定义与文件内容不一致。")
                batch_ids.add(document["save_id"])
                lookup = {item["id"]: item["name"] for item in document["labels"]}
                annotations[axis] = [{"id": item["id"], "label": lookup[item["label_id"]], "kind": item.get("kind", "interval"), "start_ms": item["start_ms"], "end_ms": item["end_ms"], **({"mode": item.get("mode", "state")} if axis == "category" else {}), **({key: item[key] for key in ("created_by", "created_by_name", "created_at", "updated_by", "updated_by_name", "updated_at") if key in item} if version == 3 else {})} for item in document["segments"]]
            if len(batch_ids) != 1 or not next(iter(batch_ids)) or len(versions) != 1:
                raise HTTPException(409, "标注 JSON 不属于同一保存批次或格式版本。请恢复一致的文件后再打开。")
            version = next(iter(versions))
            expected_axes = LEGACY_AXES if version == 1 else AXES
            if set(documents) != set(expected_axes) | expected_custom:
                raise HTTPException(409, "原目录标注文件不齐全或格式版本不一致。旧版须有三个文件，新版须有四个文件。")
            # Previously exported partial annotations remain readable for manual
            # completion. Current files require posture coverage; scene and
            # category may contain unannotated intervals after writeback.
            cleaned = validate_annotations(annotations, project["duration_ms"], final=True, videos=active_videos(selected),
                                           require_coverage=version >= 2, require_scene_coverage=False,
                                           custom_tracks=tracks)
            project['_skipped_video_names'] = names
            return (cleaned, hashes, tracks) if include_tracks else (cleaned, hashes)
        except (KeyError, ValueError, TypeError):
            raise HTTPException(422, "已有标注文件格式不受支持，或时间基准不正确。")

    def open_path(self, raw_path: str, name: str | None = None, *, allowed_project_ids: set[str] | None = None) -> dict:
        name = validate_project_name(name, allow_empty=True)
        if not raw_path or not raw_path.strip():
            raise HTTPException(422, "请输入服务器可访问的原视频目录或共享目录路径。")
        path = Path(raw_path.strip().strip('"')).expanduser()
        try:
            path = path.resolve(strict=True)
            if not path.is_dir():
                raise HTTPException(422, "请选择目录，而不是单个文件。")
            if self.is_managed_copy(path) or CACHE_FOLDER in path.parts:
                raise HTTPException(422, "请选择原视频目录，不要选择平台导入副本或缓存目录。")
            if path.name.casefold() == "fpv":
                source_dir, video_dir = path.parent, path
            else:
                source_dir = path
                fpv = next((child for child in path.iterdir() if child.is_dir() and child.name.casefold() == "fpv"), None)
                video_dir = fpv or path
            paths = [item for item in video_dir.iterdir() if item.is_file() and item.suffix.casefold() in VIDEO_EXTENSIONS]
        except OSError:
            raise HTTPException(422, "无法访问目录。请检查服务器路径、网络及共享目录权限。")
        with self.lock:
            source_key = os.path.normcase(str(source_dir))
            with self.connection() as con:
                row = con.execute("SELECT document FROM projects WHERE source_key=? ORDER BY updated_at DESC LIMIT 1", (source_key,)).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous and allowed_project_ids is not None and previous["id"] not in allowed_project_ids:
                raise HTTPException(403, "该采集目录已由其他标注员领取。")
            if self.source_overlap(paths, previous["id"] if previous else None):
                raise HTTPException(409, "目录内的视频已有其他平台项目，未创建重复标注项目。")
            if previous and previous.get("_deleting"):
                raise HTTPException(409, "项目正在删除，请先完成清理。")
            if previous and previous.get("_supplemented"):
                return self.reopen_supplemented(previous, paths)
            fresh = self.create(paths, source_dir.name, source_dir, previous["id"] if previous else None)
            if previous and fresh["source_fingerprint"] not in {previous["source_fingerprint"], *previous.get("_source_fingerprint_aliases", [])}:
                raise HTTPException(409, "该采集的视频顺序、时长或文件已改变。本机草稿仍保留，未将旧标注套用到新素材。请恢复原素材，或使用其他目录创建项目。")
            imported, hashes, imported_tracks = self.read_external(previous or fresh, include_tracks=True)
            if previous:
                preserved_alignment_warnings = alignment_warnings(previous["videos"])
                previous["warnings"] = preserved_alignment_warnings
                if hashes != self.expected_external_hashes(previous):
                    if previous.get("draft_dirty"):
                        previous["warnings"] = preserved_alignment_warnings + ["原目录 JSON 已被修改。本机草稿已保留；写回将阻止覆盖外部更改。"]
                    else:
                        previous["annotations"] = imported or empty_annotations()
                        previous["custom_tracks"] = imported_tracks
                        previous['_skipped_video_names'] = fresh.get('_skipped_video_names', {})
                        previous["_external_hashes"] = hashes
                        previous["revision"] += 1
                        previous["updated_at"] = now()
                        previous["warnings"] = preserved_alignment_warnings
                self.save(previous)
                return self.public(previous)
            # For directory imports, the selected folder is the default project name.
            # Keep the marker so a later storage migration does not replace it with a date.
            fresh.update(name=name if name is not None else validate_project_name(path.name), _custom_name=True)
            fresh["annotations"] = imported or empty_annotations()
            fresh["custom_tracks"] = imported_tracks
            fresh["_external_hashes"] = hashes
            self.attach_source_caches(fresh)
            self.save(fresh)
            return self.public(fresh)

    @project_locked
    def rename_project(self, ident: str, name: str, expected_revision: int) -> dict:
        name = validate_project_name(name)
        project = self.load(ident)
        if project["revision"] != expected_revision:
            raise HTTPException(409, "项目已在其他窗口更新，请重新打开后再重命名。")
        if project["name"] == name:
            return self.public(project)
        project.update(name=name, _custom_name=True, revision=project["revision"] + 1, updated_at=now())
        self.save(project)
        return self.public(project)

    def update_draft(self, ident: str, annotations: Any, expected_revision: int, *, actor: dict | None = None) -> dict:
        with self.lock:
            project = self.load(ident)
            if project["revision"] != expected_revision:
                raise HTTPException(409, "草稿已在其他窗口更新。请重新打开项目后继续，避免覆盖最新标注。")
            if isinstance(annotations, dict) and set(annotations) in (set(LEGACY_AXES), set(AXES)):
                # An already open client must never erase newer axes.
                annotations = {**project["annotations"], **annotations}
            cleaned = validate_annotations(annotations, project["duration_ms"], videos=active_videos(project),
                                           custom_tracks=project.get("custom_tracks", []))
            events = []
            if actor is not None:
                recorded_at = now()
                for axis in cleaned:
                    old = {item["id"]: item for item in project["annotations"].get(axis, [])}
                    new = {item["id"]: item for item in cleaned[axis]}
                    for item_id in old.keys() | new.keys():
                        before, after = old.get(item_id), new.get(item_id)
                        if after is not None:
                            # Attribution from the browser is never authoritative.
                            for key in ("created_by", "created_by_name", "created_at", "updated_by", "updated_by_name", "updated_at"):
                                after.pop(key, None)
                            if before is not None:
                                for key in ("created_by", "created_by_name", "created_at", "updated_by", "updated_by_name", "updated_at"):
                                    if key in before:
                                        after[key] = before[key]
                            if before is None:
                                after.update(created_by=actor["id"], created_by_name=actor["display_name"], created_at=recorded_at)
                            elif any(before.get(key) != after.get(key) for key in ("label", "kind", "mode", "start_ms", "end_ms")):
                                after.update(updated_by=actor["id"], updated_by_name=actor["display_name"], updated_at=recorded_at)
                        if before != after:
                            events.append((uuid.uuid4().hex, ident, project["revision"] + 1, axis, item_id,
                                           "create" if before is None else "delete" if after is None else "update",
                                           actor["id"], actor["display_name"], recorded_at,
                                           json.dumps(before, ensure_ascii=False) if before else None,
                                           json.dumps(after, ensure_ascii=False) if after else None))
            project["annotations"] = cleaned
            project["revision"] += 1
            project["updated_at"] = now()
            project["draft_dirty"] = True
            if actor is None:
                self.save(project)
            else:
                with self.connection() as con:
                    con.execute("INSERT INTO projects VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_key=excluded.source_key, document=excluded.document, updated_at=excluded.updated_at", (project["id"], project.get("_source_key"), json.dumps(project, ensure_ascii=False), project["updated_at"]))
                    con.executemany("INSERT INTO edit_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", events)
            return self.public(project)

    def add_custom_track(self, ident: str, name: str, mode: str, labels: list[str], expected_revision: int) -> dict:
        with self.lock:
            project = self.load(ident)
            if project["revision"] != expected_revision:
                raise HTTPException(409, "项目已在其他窗口更新，请重新打开后添加时间轴。")
            track = {"id": "custom_" + uuid.uuid4().hex, "name": name, "mode": mode, "labels": labels}
            tracks = validate_custom_tracks([*project.get("custom_tracks", []), track])
            project["custom_tracks"] = tracks
            project["annotations"][track["id"]] = []
            project["revision"] += 1
            project["updated_at"] = now()
            project["draft_dirty"] = True
            self.save(project)
            return self.public(project)

    def delete_custom_track(self, ident: str, track_id: str, expected_revision: int) -> dict:
        with self.lock:
            project = self.load(ident)
            if project["revision"] != expected_revision:
                raise HTTPException(409, "项目已在其他窗口更新，请重新打开后删除时间轴。")
            tracks = validate_custom_tracks(project.get("custom_tracks", []))
            if track_id not in {track["id"] for track in tracks}:
                raise HTTPException(404, "找不到这条自定义时间轴。")
            project["custom_tracks"] = [track for track in tracks if track["id"] != track_id]
            project["annotations"].pop(track_id, None)
            # Retain the last external hash until writeback verifies and removes
            # the old JSON. This protects a concurrent NAS edit from deletion.
            project["revision"] += 1
            project["updated_at"] = now()
            project["draft_dirty"] = True
            self.save(project)
            return self.public(project)

    def edit_history(self, ident: str, *, limit: int = 200) -> list[dict]:
        self.load(ident)
        with self.connection() as con:
            rows = con.execute("SELECT id,revision,axis,segment_id,action,actor_id,actor_name,recorded_at,before_json,after_json FROM edit_events WHERE project_id=? ORDER BY revision DESC,id DESC LIMIT ?", (ident, limit)).fetchall()
        return [{"id": row[0], "revision": row[1], "axis": row[2], "segment_id": row[3], "action": row[4], "actor_id": row[5], "actor_name": row[6], "recorded_at": row[7], "before": json.loads(row[8]) if row[8] else None, "after": json.loads(row[9]) if row[9] else None} for row in rows]

    @staticmethod
    def current_source_fingerprint(project: dict) -> str:
        """Fingerprint a fresh import of the verified originals, retaining the historical ID."""
        if "_sources" not in project:
            # Public snapshots intentionally omit private filesystem identities.
            return project["source_fingerprint"]
        source_dir = Path(project["source_dir"]) if project.get("source_dir") else None
        items = []
        for index, video in enumerate(project["videos"]):
            source = project["_sources"][video["id"]]
            path = Path(source["path"])
            relative = str(path.relative_to(source_dir)).replace("\\", "/") if source_dir and path.is_relative_to(source_dir) else path.name
            items.append({**video, "id": f"v{index + 1:04d}", "name": path.name, "relative_path": relative,
                          "url": None, "thumbnail_url": None, "stamp": source["stamp"]})
        return digest(document_bytes(items))

    def export_documents(self, project: dict, *, for_writeback: bool = False) -> tuple[str, dict[str, bytes]]:
        annotations = validate_annotations(project["annotations"], project["duration_ms"], final=True,
                                           videos=active_videos(project), require_scene_coverage=not for_writeback,
                                           custom_tracks=project.get("custom_tracks", []))
        save_id = uuid.uuid4().hex
        recording_start = datetime.fromisoformat(project["recording_start"]).astimezone(timezone(timedelta(hours=8)))
        timebase = {"unit": "ms", "origin": "recording_datetime", "real_time_format": "HHMMSS", "recording_start": project["recording_start"], "timezone": "Asia/Shanghai", "gaps": project["gaps"], "interval": "[start,end)", "point_semantics": "instant; start_ms=end_ms", "duration_ms": project["duration_ms"], "source_fingerprint": project["source_fingerprint"], "videos": [{key: video[key] for key in ("id", "relative_path", "recording_start", "original_media_start_ms", "duration_ms", "start_ms", "end_ms")} for video in project["videos"]]}
        current_fingerprint = self.current_source_fingerprint(project)
        if current_fingerprint != project["source_fingerprint"]:
            # Copy uploads can have different filesystem mtimes and FPV-relative
            # paths. The fresh-import alias still binds the exact stored source
            # stamps and complete timeline; it does not weaken mismatch checks.
            timebase["source_fingerprint_aliases"] = [current_fingerprint]
        runs, bridges, gaps = selected_recording_layout(project)
        if project.get('_skipped_video_names'):
            timebase['skipped_videos'] = skipped_videos(project)
        timebase["gaps"] = gaps
        timebase["recording_runs"] = runs
        timebase["continuity_bridges"] = bridges
        if bridges:
            timebase["continuity_policy"] = {
                "name": "adjacent_camera_second_precision_v2",
                "timestamp_resolution_ms": 1000,
                "maximum_seam_exclusive_ms": CAMERA_SEAM_EXCLUSIVE_MS,
                "maximum_raw_boundary_error_exclusive_ms": CAMERA_SEAM_EXCLUSIVE_MS,
                "maximum_alignment_offset_inclusive_ms": MAX_CAMERA_ALIGNMENT_MS,
                "scope": "same_camera_consecutive_index_increasing_filename_time",
                "preserves_video_and_annotation_coordinates": True,
                "note": "Subsecond seams are treated as camera timestamp rounding for continuous annotation; media coordinates and original timestamps remain unchanged.",
            }
        aligned_videos = [video for video in project["videos"] if video.get("alignment_offset_ms", 0) > 0]
        if aligned_videos:
            for mapping, video in zip(timebase["videos"], project["videos"]):
                for key in ("filename_recording_start", "filename_start_ms", "alignment_offset_ms"):
                    if key in video:
                        mapping[key] = video[key]
            timebase["alignment_policy"] = {
                "name": "adjacent_camera_second_precision_v2",
                "timestamp_resolution_ms": 1000,
                "maximum_raw_overlap_inclusive_ms": MAX_CAMERA_OVERLAP_MS,
                "maximum_cumulative_offset_inclusive_ms": MAX_CAMERA_ALIGNMENT_MS,
                "maximum_nominal_gap_exclusive_ms": CAMERA_SEAM_EXCLUSIVE_MS,
                "raw_boundary_error": "previous_filename_start_ms + previous_original_duration_ms - filename_start_ms",
                "scope": "same_camera_consecutive_index_increasing_filename_time",
                "effective_start": "max(filename_start_ms, previous_video_end_ms)",
                "preserves_full_video_duration": True,
                "shifted_video_count": len(aligned_videos),
                "maximum_applied_offset_ms": max(video["alignment_offset_ms"] for video in aligned_videos),
                "note": "recording_start is an effective playback coordinate; filename_recording_start preserves the original second-resolution timestamp, not measured subsecond timing.",
            }
        custom_tracks = validate_custom_tracks(project.get("custom_tracks", []))
        if custom_tracks:
            timebase["custom_tracks"] = custom_tracks
        custom = {track["id"]: track for track in custom_tracks}
        result = {}
        for axis in annotations:
            used_labels = {item["label"] for item in annotations[axis]}
            # Retired scene options remain valid only as legacy data in exports;
            # keep their established label IDs when those annotations are used.
            labels = {label: ident for label, ident in FIXED_LABELS.get(axis, {}).items()
                      if axis != "scene" or label in ("室内", "室外") or label in used_labels}
            if axis in custom and custom[axis]["mode"] == "state":
                labels = {label: "custom_label_" + digest(label.encode("utf-8"))[:16]
                          for label in custom[axis]["labels"]}
            records = []
            for item in annotations[axis]:
                label = item["label"]
                label_id = labels.setdefault(label, "habit_" + digest(label.encode("utf-8"))[:16])
                start_at = recording_start + timedelta(milliseconds=item["start_ms"])
                end_at = recording_start + timedelta(milliseconds=item["end_ms"])
                # Keep HHMMSS as a string to preserve leading zeroes; original
                # millisecond offsets retain sub-second precision and gap timing.
                records.append({
                    **item, "label_id": label_id,
                    "start_time": start_at.strftime("%H%M%S"),
                    "end_time": end_at.strftime("%H%M%S"),
                    "start_date": start_at.date().isoformat(),
                    "end_date": end_at.date().isoformat(),
                })
            document = {"schema_version": 3, "save_id": save_id, "saved_at": now(), "collection_id": project["id"], "collection_name": project["name"], "axis": axis, "timebase": timebase, "labels": [{"id": value, "name": label} for label, value in labels.items()], "segments": records}
            if axis in custom:
                document["axis_name"] = custom[axis]["name"]
                document["axis_mode"] = custom[axis]["mode"]
            result[axis] = document_bytes(document)
        return save_id, result

    def export_zip(self, ident: str) -> bytes:
        project = self.load(ident)
        _, documents = self.export_documents(project)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for axis, payload in documents.items():
                archive.writestr(filename_for_axis(axis), payload)
        return buffer.getvalue()

    @staticmethod
    def expected_external_hashes(project: dict) -> dict:
        previous = project.get("_external_hashes", {})
        return {**{axis: previous.get(axis) for axis in AXES},
                **{axis: value for axis, value in previous.items() if axis.startswith("custom_") and value is not None}}

    @staticmethod
    def external_directory(source: Path) -> Path:
        directory = source / "timeline"
        try:
            if directory.exists() and not directory.is_dir():
                raise HTTPException(409, "原目录的 timeline 已存在且不是文件夹，请先处理同名文件。")
            # Choose one complete location; never mix new and legacy batches.
            return directory if any((directory / name).exists() for name in FILENAMES.values()) else source
        except OSError:
            raise HTTPException(409, "无法检查 timeline 文件夹，请检查共享网络和权限。")

    def current_external_hashes(self, source: Path) -> dict:
        try:
            directory = self.external_directory(source)
            hashes = {axis: digest((directory / FILENAMES[axis]).read_bytes()) if (directory / FILENAMES[axis]).exists() else None for axis in AXES}
            for path in directory.glob("custom_*.timeline.json"):
                axis = path.name.removesuffix(".timeline.json")
                if re.fullmatch(r"custom_[0-9a-f]{32}", axis):
                    if path.is_symlink() or not path.is_file():
                        raise HTTPException(409, "自定义时间轴文件不是普通文件，已停止写回。")
                    hashes[axis] = digest(path.read_bytes())
            return hashes
        except OSError:
            raise HTTPException(409, "无法检查原目录已有 JSON，未进行写回。请检查共享网络和权限。")

    def verify_sources(self, project: dict) -> None:
        for value in project["_sources"].values():
            if self.source_stamp(Path(value["path"])) != value["stamp"]:
                raise HTTPException(409, "原视频已更改，停止写回以避免错误时间基准。")

    def writeback(self, ident: str, *, expected_revision: int | None = None) -> dict:
        with self.lock:
            project = self.load(ident)
            if expected_revision is not None and project["revision"] != expected_revision:
                raise HTTPException(409, "项目已在其他窗口更新，请重新打开并核对后再提交。")
            if not project["source_dir"]:
                raise HTTPException(400, "项目尚未关联统一的原采集目录，请导出时间轴 JSON 后自行保存。")
            if self.remote and self.remote.map_path(project['source_dir']) is not None:
                # Local validation is preserved; the server performs source/conflict checks and commits.
                _, documents = self.export_documents(project, for_writeback=True)
                response = self.remote.writeback(project)
                for field in ('_external_hashes', 'draft_dirty', 'warnings', 'last_writeback', 'updated_at'):
                    project[field] = response['project'][field]
                self.save(project)
                result = dict(response['result'])
                result['paths'] = [str(Path(project['source_dir']) / 'timeline' / filename_for_axis(axis)) for axis in documents]
                return result
            save_id, documents = self.export_documents(project, for_writeback=True)
            self.verify_sources(project)
            source = Path(project["source_dir"])
            expected = self.expected_external_hashes(project)
            if self.current_external_hashes(source) != expected:
                raise HTTPException(409, "原目录 JSON 已被其他程序修改。为保护外部结果，本次没有写回；本机草稿保留，可下载 JSON。")
            destination = source / "timeline"
            target_expected = expected if self.external_directory(source) == destination else {axis: None for axis in documents}
            retired = [axis for axis, value in target_expected.items()
                       if axis.startswith("custom_") and axis not in documents and value is not None]
            temporary, backups, replaced, removed = {}, {}, [], []
            backup_dir = source / ".annotation-backups" / save_id
            try:
                destination.mkdir(exist_ok=True)
                if any(value is not None for value in target_expected.values()):
                    backup_dir.mkdir(parents=True, exist_ok=False)
                for axis in documents:
                    filename = filename_for_axis(axis)
                    target = destination / filename
                    if target.exists():
                        backup = backup_dir / filename
                        shutil.copy2(target, backup)
                        backups[axis] = backup
                    temp = destination / ("." + filename + "." + save_id + ".tmp")
                    temporary[axis] = temp
                    with temp.open("xb") as handle:
                        handle.write(documents[axis])
                        handle.flush()
                        os.fsync(handle.fileno())
                for axis in retired:
                    filename = filename_for_axis(axis)
                    backup = backup_dir / filename
                    shutil.copy2(destination / filename, backup)
                    backups[axis] = backup
                if self.current_external_hashes(source) != expected:
                    raise HTTPException(409, "准备写回时发现原目录 JSON 有变化，已停止写回。")
                for axis in documents:
                    # Check each target immediately before replacing. A network share
                    # cannot provide a transaction across independent files.
                    target = destination / filename_for_axis(axis)
                    current = digest(target.read_bytes()) if target.exists() else None
                    if current != target_expected.get(axis):
                        raise OSError("target changed during writeback")
                    os.replace(temporary[axis], target)
                    replaced.append(axis)
                for axis in retired:
                    target = destination / filename_for_axis(axis)
                    if digest(target.read_bytes()) != target_expected[axis]:
                        raise OSError("retired axis changed during writeback")
                    target.unlink()
                    removed.append(axis)
                if self.current_external_hashes(source) != {axis: digest(payload) for axis, payload in documents.items()}:
                    raise OSError("writeback read verification failed")
            except (OSError, HTTPException) as exc:
                rollback_failed = []
                for axis in reversed(removed):
                    filename = filename_for_axis(axis)
                    target = destination / filename
                    try:
                        if target.exists():
                            rollback_failed.append(filename)
                            continue
                        shutil.copy2(backups[axis], target)
                    except OSError:
                        rollback_failed.append(filename)
                for axis in reversed(replaced):
                    filename = filename_for_axis(axis)
                    target = destination / filename
                    try:
                        # Do not overwrite a concurrent external update while rolling back.
                        if digest(target.read_bytes()) != digest(documents[axis]):
                            rollback_failed.append(filename)
                            continue
                        if axis in backups:
                            rollback = destination / ("." + filename + ".rollback." + save_id)
                            shutil.copy2(backups[axis], rollback)
                            os.replace(rollback, target)
                        else:
                            target.unlink()
                    except OSError:
                        rollback_failed.append(filename)
                if rollback_failed:
                    project["warnings"] = ["写回中断且部分文件未能恢复：" + "、".join(rollback_failed) + "。本机草稿保留；请检查原目录和 .annotation-backups。"]
                    self.save(project)
                    raise HTTPException(500, project["warnings"][0])
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(500, "写回失败，已恢复本次替换的文件。本机草稿已保留，请检查共享目录写入权限和网络后重试。")
            finally:
                for temp in temporary.values():
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass
            project["_external_hashes"] = {axis: digest(payload) for axis, payload in documents.items()}
            project["draft_dirty"] = False
            project["warnings"] = alignment_warnings(project["videos"])
            project["last_writeback"] = {"save_id": save_id, "saved_at": now()}
            project["updated_at"] = now()
            self.save(project)
            return {"paths": [str(destination / filename_for_axis(axis)) for axis in documents], "save_id": save_id}

    def media_source(self, project: dict, video_id: str) -> tuple[Path, dict]:
        source = project["_sources"].get(video_id)
        if not source:
            raise HTTPException(404, "找不到这个视频。")
        path = Path(source["path"])
        self.check_media_source(path, source)
        return path, source

    @staticmethod
    def source_available(path: Path, source: dict) -> bool:
        try:
            stat = path.stat()
        except OSError:
            # An unplugged card or disconnected share does not invalidate a
            # completed local preview for the saved source identity.
            return False
        if not path.is_file():
            return False
        if stat.st_size != source["stamp"]["size"] or stat.st_mtime_ns != source["stamp"]["mtime_ns"]:
            raise HTTPException(409, "原视频已更改，请恢复原素材后重新打开项目。")
        return True

    @staticmethod
    def check_media_source(path: Path, source: dict) -> None:
        if not ProjectService.source_available(path, source):
            raise HTTPException(404, "原视频不可访问，且所需预览尚未缓存。请重新连接 SD 卡或恢复素材路径后准备预览。")

    def preview_spec(self, project: dict, video_id: str, legacy: bool = False, check_source: bool = True, *, create_cache: bool = True) -> PreviewSpec:
        source = project["_sources"].get(video_id)
        if not source:
            raise HTTPException(404, "找不到这个视频。")
        # create() stores the resolved absolute path. Do not resolve it again
        # through an absent/remounted drive when locating an existing cache.
        path = Path(source["path"])
        if not legacy and check_source:
            self.source_available(path, source)
        # Timeline offsets, project IDs and vNNNN numbering do not affect media.
        identity = {"profile": "superfast-original-gop30-v1", "path": os.path.normcase(source.get("cache_identity_path", str(path))),
                    "stamp": source.get("cache_identity_stamp", source["stamp"]), "duration_ms": source["duration_ms"],
                    "media_start_seconds": source.get("media_start_seconds", 0)}
        key = "p2_" + digest(document_bytes(identity))
        legacy_key = project.get("_legacy_cache_fingerprints", {}).get(video_id, project["source_fingerprint"]) + "_" + video_id
        directory = self.cache if legacy else self.storage.directory(project["id"]) / "preview"
        if not legacy and source.get("cache_directory"):
            directory = self.source_cache.validate(path, project["id"], source["cache_directory"])
            if create_cache and not directory.is_dir():
                directory = self.source_cache.ensure(path, project["id"])
        elif not legacy and create_cache:
            directory.mkdir(parents=True, exist_ok=True)
        scoped_key = key if legacy else project["id"] + "_" + key
        if not legacy:
            self.storyboards.register_directory(scoped_key, directory)
        return PreviewSpec(scoped_key, path, source,
                           directory / (key + ".mp4"), directory / (key + ".error.json"),
                           thumbnail_target=directory / (key + ".jpg"),
                           legacy_preview=directory / (legacy_key + ".mp4"), legacy_thumbnail=directory / (legacy_key + ".jpg"),
                           project_id=None if legacy else project["id"])

    def cached_preview(self, project: dict, video_id: str, spec: PreviewSpec) -> Path | None:
        return self.cached_preview_for_spec(spec)

    @staticmethod
    def cached_preview_for_spec(spec: PreviewSpec) -> Path | None:
        source, path = spec.source, spec.path
        compatible = abs(source.get("media_start_seconds", 0)) < 0.001 and abs(source.get("format_start_seconds", 0)) < 0.001 and path.suffix.casefold() in {".mp4", ".m4v", ".mov"} and source["codec"] == "h264" and source["pixel_format"] in {"yuv420p", "yuvj420p"} and all(codec in {"aac", "mp3"} for codec in source["audio_codecs"])
        available = ProjectService.source_available(path, source)
        for candidate in (spec.target, spec.legacy_preview):
            if candidate is not None and candidate.is_file() and candidate.stat().st_size:
                return candidate
        # A LAN browser must never receive the original file through the
        # compatibility shortcut; prepare an actual preview first.
        if compatible and available and not os.getenv("DATAMARK_ORIGIN"):
            return path
        return None

    @staticmethod
    def valid_thumbnail(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            with path.open("rb") as handle:
                if handle.read(2) != b"\xff\xd8":
                    return False
                handle.seek(-2, 2)
                return handle.read(2) == b"\xff\xd9"
        except OSError:
            return False

    def cached_thumbnail(self, spec: PreviewSpec) -> Path | None:
        for candidate in (spec.thumbnail_target, spec.legacy_thumbnail):
            if self.valid_thumbnail(candidate):
                return candidate
        return None

    def register_legacy_cache(self, spec: PreviewSpec) -> None:
        # Same-volume hardlinks preserve completed media without copying large files.
        pairs = [(self.cached_preview_for_spec(spec), spec.target), (self.cached_thumbnail(spec), spec.thumbnail_target)]
        for cached, target in pairs:
            if cached is not None and target is not None and cached.parent == target.parent and cached != target and not target.exists():
                try:
                    os.link(cached, target)
                except OSError:
                    pass

    def preparation_status(self, ident: str) -> dict:
        # Only the SQLite snapshot needs the project mutation lock. A slow NAS
        # scan never holds it and is never allowed to create cache directories.
        with self.lock:
            project = self.load(ident)
        signature = digest(document_bytes([project["source_fingerprint"], project["_sources"]]))
        with self.previews.condition:
            signature += repr(sorted((key, item["state"]) for key, item in self.previews.jobs.items()
                                     if item["spec"].project_id == ident))
        intent = self.storage.directory(ident) / "preparation.json"
        try:
            signature += str(intent.stat().st_mtime_ns)
        except OSError:
            pass
        network = any(str(item.get("cache_directory", item["path"])).startswith("\\\\")
                      for item in project["_sources"].values())
        ttl = 3.0 if network else 0.0
        with self._preparation_guard:
            entry = self._preparation_scans.get(ident)
            if entry and not entry["running"] and entry["signature"] == signature and time.monotonic() - entry["at"] < ttl:
                return copy.deepcopy(entry["result"])
            if not entry or not entry["running"]:
                event = threading.Event()
                entry = {"running": True, "signature": signature, "event": event, "result": None, "at": 0.0}
                self._preparation_scans[ident] = entry
                def scan():
                    try:
                        result = self._scan_preparation_status(project)
                    except Exception:
                        LOGGER.exception("Could not inspect preview caches")
                        result = self._checking_preparation(project)
                    with self._preparation_guard:
                        entry.update(running=False, result=result, at=time.monotonic())
                        event.set()
                threading.Thread(target=scan, name="preview-cache-check", daemon=True).start()
            event = entry["event"]
        # One scan per project, even when a browser retries. Never tie up the
        # request pool while waiting for dozens of network media files.
        event.wait(.4)
        with self._preparation_guard:
            if not entry["running"] and entry["signature"] == signature and entry["result"]:
                return copy.deepcopy(entry["result"])
        return self._checking_preparation(project)

    @staticmethod
    def _checking_preparation(project: dict) -> dict:
        return {"project_id": project["id"], "state": "running", "requested": False,
                "checking": True, "total": len(project["videos"]), "ready": 0, "failed": 0,
                "running": 0, "queued": 0, "progress": 0,
                "items": [{"video_id": video["id"], "name": video["name"], "state": "checking",
                           "progress": None, "detail": "正在检查已有预览缓存，视频播放和标注可继续。"}
                          for video in project["videos"]]}

    def _scan_preparation_status(self, project: dict) -> dict:
        ident = project["id"]
        manifest_path = self.storage.directory(project["id"]) / "preparation.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            has_intent = manifest.get("source_fingerprint") == project["source_fingerprint"]
        except (OSError, ValueError, TypeError, AttributeError):
            has_intent = False
        items = []
        for video in project["videos"]:
            try:
                spec = self.preview_spec(project, video["id"], create_cache=False)
                video_ready = self.cached_preview_for_spec(spec) is not None
                thumbnail_ready = self.cached_thumbnail(spec) is not None
                storyboard_ready = self.storyboards.read(spec.key, spec.source["duration_ms"]) is not None
                fast_ready = self.cached_fast_preview(spec) is not None
                if video_ready and thumbnail_ready and storyboard_ready and fast_ready:
                    status = {"state": "ready", "progress": 100, "detail": None}
                else:
                    if not video_ready:
                        self.check_media_source(spec.path, spec.source)
                    status = self.previews.status(spec)
                    if status["state"] == "ready":
                        status = {"state": "idle", "progress": None, "detail": "部分预览缓存尚未完成，可以继续准备。"}
                    if video_ready and status["state"] != "error":
                        status["progress"] = max(status["progress"] or 0, 95)
                        if status["state"] == "idle":
                            status["detail"] = "视频可播放，尚需准备悬停图片或高倍速预览。" if thumbnail_ready else "视频预览已就绪，尚需准备缩略图。"
                        elif status["state"] == "running" and not fast_ready:
                            status["detail"] = "正在生成高倍速预览；普通播放和标注仍可使用。"
                        elif status["state"] == "running" and thumbnail_ready and not storyboard_ready:
                            status["detail"] = "正在生成悬停图片；视频已就绪，可继续标注。"
            except HTTPException as exc:
                status = {"state": "error", "progress": None, "detail": str(exc.detail)}
            except OSError:
                status = {"state": "error", "progress": None, "detail": "无法检查素材或预览缓存，请检查磁盘和共享网络。"}
            items.append({"video_id": video["id"], "name": video["name"], **status})
        counts = {name: sum(item["state"] == state for item in items) for name, state in
                  (("ready", "ready"), ("failed", "error"), ("running", "running"), ("queued", "queued"))}
        total = len(items)
        if counts["ready"] == total:
            state = "ready"
        elif counts["running"] or counts["queued"]:
            state = "running"
        elif has_intent:
            state = "partial" if counts["ready"] + counts["failed"] == total else "paused"
        else:
            state = "idle"
        return {"project_id": ident, "state": state, "requested": has_intent, "total": total, **counts,
                "progress": round(sum(item["progress"] or 0 for item in items) / total, 1) if total else 100,
                "items": items}

    @project_locked
    def prepare_project(self, ident: str, retry_failed: bool = False) -> dict:
        project = self.load(ident)
        manifest = {"project_id": project["id"], "source_fingerprint": project["source_fingerprint"],
                    "requested_at": now(), "video_ids": [video["id"] for video in project["videos"]]}
        with self.lock:
            self.storage.directory(project["id"]).mkdir(parents=True, exist_ok=True)
            target = self.storage.directory(project["id"]) / "preparation.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(document_bytes(manifest))
            os.replace(temporary, target)
        specs = []
        for video in project["videos"]:
            try:
                spec = self.preview_spec(project, video["id"])
                self.register_legacy_cache(spec)
                video_ready = self.cached_preview_for_spec(spec) is not None
                if not video_ready:
                    self.check_media_source(spec.path, spec.source)
                if not video_ready or self.cached_thumbnail(spec) is None or self.cached_fast_preview(spec) is None or self.storyboards.read(spec.key, spec.source["duration_ms"]) is None:
                    specs.append(spec)
            except (HTTPException, OSError):
                # Status reports each inaccessible/changed source independently.
                continue
        self.previews.request_batch(project["id"], specs, retry_failed=retry_failed)
        return self.preparation_status(ident)

    def preview_status(self, ident: str, video_id: str, fast: bool = False) -> dict:
        # Inspect a snapshot without holding the project lock across NAS reads.
        # A status GET must not recreate cache folders after concurrent deletion.
        with self.lock:
            project = self.load(ident)
        spec = self.preview_spec(project, video_id, create_cache=False)
        cached = self.cached_fast_preview(spec) if fast else self.cached_preview(project, video_id, spec)
        if cached:
            suffix = "?fast=true" if fast else ""
            return {"state": "ready", "progress": 100, "url": f"/api/media/{ident}/{video_id}{suffix}", "detail": None}
        if self.cached_preview_for_spec(spec) is None:
            self.check_media_source(spec.path, spec.source)
        status = self.previews.status(spec)
        if status["state"] == "ready":
            status = {"state": "idle", "progress": None, "detail": "预览缓存已被移除，可重新生成。"}
        return {**status, "url": None}

    @project_locked
    def prepare_preview(self, ident: str, video_id: str, prefetch: bool = True, retry: bool = False, fast: bool = False) -> dict:
        project = self.load(ident)
        current = self.preview_spec(project, video_id)
        index = next(index for index, video in enumerate(project["videos"]) if video["id"] == video_id)
        ids = [video_id] + ([video["id"] for video in project["videos"][index + 1:index + 3]] if prefetch else [])
        specs = []
        for candidate in ids:
            try:
                spec = current if candidate == video_id else self.preview_spec(project, candidate)
                cached = self.cached_preview(project, candidate, spec)
                if cached:
                    self.register_legacy_cache(spec)
                    if self.cached_thumbnail(spec) is None or self.cached_fast_preview(spec) is None or self.storyboards.read(spec.key, spec.source["duration_ms"]) is None:
                        specs.append(spec)
                else:
                    self.check_media_source(spec.path, spec.source)
                    specs.append(spec)
            except (HTTPException, OSError):
                if candidate == video_id:
                    raise
                # Inaccessible lookahead must not prevent the current clip playing.
        self.previews.request(specs, retry_key=current.key if retry else None)
        return self.preview_status(ident, video_id, fast=fast)

    def render_preview(self, spec: PreviewSpec, update, stopping: threading.Event) -> None:
        self.render_video(spec, lambda value: update(value * 0.88), stopping)
        if stopping.is_set():
            raise HTTPException(503, "平台关闭，预览准备已暂停，可继续准备。")
        self.render_thumbnail(spec)
        update(90)
        self.render_fast_preview(spec, lambda value: update(90 + value * 0.05), stopping)
        self.render_storyboard(spec, lambda value: update(95 + value * 0.04), stopping)

    def render_storyboard(self, spec: PreviewSpec, update, stopping: threading.Event) -> None:
        if self.storyboards.read(spec.key, spec.source["duration_ms"]):
            update(100)
            return
        cached = self.cached_preview_for_spec(spec)
        if cached is None:
            self.check_media_source(spec.path, spec.source)
            raise HTTPException(409, "请先完成视频预览，再生成悬停图片。")
        ffmpeg = self.tool("ffmpeg")
        if not ffmpeg:
            raise HTTPException(503, "缺少 FFmpeg，无法准备悬停图片。")
        self.storyboards.render(cached, spec.key, spec.source["duration_ms"], ffmpeg, update, stopping)

    @project_locked
    def storyboard_status(self, ident: str, video_id: str) -> dict:
        project = self.load(ident)
        spec = self.preview_spec(project, video_id)
        manifest = self.storyboards.read(spec.key, spec.source["duration_ms"])
        if manifest:
            return {**manifest, "state": "ready", "detail": None,
                    "sheets": [f"/api/storyboards/{ident}/{video_id}/sheets/{index}?v=s1-{spec.key}"
                               for index in range(len(manifest["sheets"]))]}
        status = self.previews.status(spec)
        if status["state"] in {"idle", "ready"}:
            return {"state": "idle", "detail": "悬停图片尚未准备，请点击顶部准备全部预览。"}
        return {**status, "detail": status["detail"] if status["state"] == "error" else "正在准备悬停图片，视频仍可正常播放。"}

    @project_locked
    def storyboard_sheet(self, ident: str, video_id: str, index: int) -> Path:
        project = self.load(ident)
        spec = self.preview_spec(project, video_id)
        manifest = self.storyboards.read(spec.key, spec.source["duration_ms"])
        if not manifest or index < 0 or index >= len(manifest["sheets"]):
            raise HTTPException(404, "找不到这张悬停图片，请完成素材准备后重试。")
        return self.storyboards.sheet_path(spec.key, manifest["sheets"][index])

    @staticmethod
    def fast_preview_path(spec: PreviewSpec) -> Path:
        return spec.target.with_name(spec.target.stem + ".fast20-v2.mp4")

    def cached_fast_preview(self, spec: PreviewSpec) -> Path | None:
        path = self.fast_preview_path(spec)
        try:
            with path.open("rb") as handle:
                header = handle.read(32)
            if len(header) == 32 and header[4:8] == b"ftyp":
                return path
        except OSError:
            pass
        return None

    def render_fast_preview(self, spec: PreviewSpec, update, stopping: threading.Event) -> None:
        if self.cached_fast_preview(spec):
            update(100)
            return
        cached = self.cached_preview_for_spec(spec)
        if cached is None:
            raise HTTPException(409, "请先完成普通预览，再准备高倍速预览。")
        ffmpeg = self.tool("ffmpeg")
        if not ffmpeg:
            raise HTTPException(503, "缺少 FFmpeg，无法准备高倍速预览。")
        target = self.fast_preview_path(spec)
        temporary = target.with_suffix(".partial.mp4")
        duration = spec.source["duration_ms"]
        # Sample before scaling/encoding. A 20x compressed timeline at 30 fps
        # plays at native 2.5x / 5x for requested 50x / 100x, without seek loops.
        filters = ("setpts=PTS-STARTPTS,fps=3/2:start_time=0:round=up,"
                   "scale=640:360:force_original_aspect_ratio=decrease:reset_sar=1,"
                   "pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1,settb=1/30,setpts=N,"
                   "fps=30:start_time=0:round=up,trim=end_frame=" + str(math.ceil(duration * 1.5 / 1000)))
        args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-progress", "pipe:1", "-nostats", "-stats_period", "0.5",
                "-threads", "8", "-filter_threads", "1", "-i", str(cached), "-map", "0:v:0", "-an",
                "-vf", filters, "-fps_mode", "cfr", "-c:v", "libx264",
                "-preset", "veryfast", "-threads", "2", "-crf", "25", "-g", "30",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)]
        try:
            run_ffmpeg(args, max(1, round(duration / 20)), target.with_suffix(".ffmpeg.log"), update, stopping,
                       timeout_seconds=max(300, duration / 1000 * 2))
            rendered = self.probe(temporary)
            if abs(rendered.get("media_start_seconds", 0)) > .001 or abs(rendered["duration_ms"] - duration / 20) > 70:
                raise HTTPException(422, "高倍速预览时间校验失败，已停止使用，避免标注错位。")
            if stopping.is_set():
                raise HTTPException(503, "高倍速准备已暂停。")
            self.source_available(spec.path, spec.source)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def render_video(self, spec: PreviewSpec, update, stopping: threading.Event) -> None:
        if self.cached_preview_for_spec(spec) is not None:
            update(99)
            return
        self.check_media_source(spec.path, spec.source)
        ffmpeg = self.tool("ffmpeg")
        if not ffmpeg:
            raise HTTPException(503, "缺少 FFmpeg。请先运行项目安装脚本后重试。")
        temp = spec.target.with_name(spec.target.stem + ".partial.mp4")
        log_path = spec.target.with_suffix(".ffmpeg.log")
        filters = "setpts=PTS-STARTPTS,scale=trunc(iw/2)*2:trunc(ih/2)*2"
        args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-progress", "pipe:1", "-nostats", "-stats_period", "0.5", "-filter_threads", "2",
                "-threads", "2", "-copyts", "-i", str(spec.path), "-map", "0:v:0", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "superfast", "-threads", "4", "-crf", "23",
                "-g", "30", "-keyint_min", "30", "-pix_fmt", "yuv420p", "-vf", filters,
                "-af", f"asetpts=PTS-({spec.source.get('media_start_seconds', 0):.9f})/TB,atrim=start=0",
                "-fps_mode", "passthrough", "-c:a", "aac", "-movflags", "+faststart", str(temp)]
        try:
            run_ffmpeg(args, spec.source["duration_ms"], log_path, update, stopping)
            preview = self.probe(temp)
            if abs(preview.get("media_start_seconds", 0)) >= 0.001:
                raise HTTPException(422, "预览的媒体起点未能归零，已停止使用该缓存，避免标注时间错位。")
            if abs(preview["duration_ms"] - spec.source["duration_ms"]) > 250:
                raise HTTPException(422, "预览转码后的时长与原素材不一致，已停止使用该缓存。")
            self.check_media_source(spec.path, spec.source)
            os.replace(temp, spec.target)
        finally:
            temp.unlink(missing_ok=True)

    def media(self, ident: str, video_id: str, thumbnail: bool = False, fast: bool = False) -> Path:
        # Completed media is read-only. Network reads must not serialize the
        # timeline's thumbnails, playback requests and draft saves behind one lock.
        with self.lock:
            project = self.load(ident)
        spec = self.preview_spec(project, video_id, create_cache=False)
        if fast:
            cached = self.cached_fast_preview(spec)
            if cached:
                return cached
            raise HTTPException(404, "高倍速预览尚未准备完成。")
        if not thumbnail:
            cached = self.cached_preview(project, video_id, spec)
            if cached:
                return cached
            self.check_media_source(spec.path, spec.source)
            raise HTTPException(409, "预览尚未准备完成，请先等待预览状态变为就绪。")
        cached = self.cached_thumbnail(spec)
        if cached:
            return cached
        # The miss path can write files, so preserve the original mutation lock
        # and re-read the project before recreating a missing cache or rendering.
        with self.lock:
            current = self.load(ident)
            if current["_sources"] != project["_sources"]:
                raise HTTPException(409, "素材位置已更新，请重新获取缩略图。")
            spec = self.preview_spec(current, video_id)
            return self.render_thumbnail(spec)

    def render_thumbnail(self, spec: PreviewSpec) -> Path:
        cached = self.cached_thumbnail(spec)
        if cached:
            return cached
        target = spec.thumbnail_target
        if target is None:
            raise HTTPException(500, "缩略图缓存路径缺失。")
        with self.lock:
            media_lock = self.media_locks.setdefault(str(target), threading.Lock())
        with media_lock, self.thumbnail_slots:
            cached = self.cached_thumbnail(spec)
            if cached:
                return cached
            input_path = self.cached_preview_for_spec(spec)
            if input_path is None:
                self.check_media_source(spec.path, spec.source)
                input_path = spec.path
            ffmpeg = self.tool("ffmpeg")
            if not ffmpeg:
                raise HTTPException(503, "缺少 FFmpeg。请先运行项目安装脚本。")
            temp = target.with_name(target.stem + ".partial.jpg")
            args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1",
                    "-filter_threads", "1", "-ss", "0", "-i", str(input_path), "-frames:v", "1", "-vf", "scale=320:-2",
                    "-threads", "1", "-q:v", "4", str(temp)]
            try:
                self.command(args, timeout=120)
                if not self.valid_thumbnail(temp):
                    raise HTTPException(422, "缩略图生成失败，请重试。")
                if input_path == spec.path:
                    self.check_media_source(spec.path, spec.source)
                else:
                    self.source_available(spec.path, spec.source)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
            return target
