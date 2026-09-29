from __future__ import annotations

import bisect
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from fastapi import HTTPException

from .previews import run_ffmpeg

PROFILE = "storyboard-s1"
INTERVAL_MS = 1000
TILE_WIDTH = 160
TILE_HEIGHT = 90
COLUMNS = ROWS = 10
FRAMES_PER_SHEET = COLUMNS * ROWS
_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SHEET = re.compile(r"sheet-\d{5}\.jpg\Z")


def keyframe_sampling_safe(probe: dict, duration_ms: int) -> bool:
    """Every requested second must have an actual preceding keyframe within one source frame."""
    try:
        stream = probe["streams"][0]
        numerator, denominator = map(float, stream["avg_frame_rate"].split("/"))
        rate = numerator / denominator
        start = float(stream["start_time"])
        times = [float(frame["best_effort_timestamp_time"]) for frame in probe["frames"]]
        if not math.isfinite(rate) or rate <= 0 or not times or not all(math.isfinite(value) for value in times):
            return False
        # setpts starts at the first decoded frame: skipping a non-key opening frame would shift all samples.
        if not math.isfinite(start) or abs(times[0] - start) > 0.000001:
            return False
        times = [value - start for value in times]
        if any(right < left for left, right in zip(times, times[1:])):
            return False
        tolerance = min(0.05, 1 / rate) + 0.000001
        for second in range(math.ceil(duration_ms / INTERVAL_MS)):
            index = bisect.bisect_right(times, second + 0.000001) - 1
            if index < 0 or second - times[index] > tolerance:
                return False
        return True
    except (KeyError, ValueError, TypeError, IndexError, ZeroDivisionError):
        return False


def can_sample_keyframes(source: Path, duration_ms: int, ffmpeg: Path, stopping: threading.Event) -> bool:
    """Bounded, cancellable scan; unknown or sparse GOP sources retain full decoding."""
    ffprobe = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if not ffprobe.is_file() or stopping.is_set():
        return False
    args = [str(ffprobe), "-v", "error", "-threads", "2", "-skip_frame", "nokey",
            "-select_streams", "v:0", "-show_frames", "-show_entries",
            "frame=best_effort_timestamp_time:stream=avg_frame_rate,start_time", "-of", "json", str(source)]
    process = None
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=flags)
        deadline = time.monotonic() + 30
        while True:
            if stopping.is_set() or time.monotonic() >= deadline:
                return False
            try:
                output, _ = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                continue
        return process.returncode == 0 and keyframe_sampling_safe(json.loads(output), duration_ms)
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()


class StoryboardCache:
    """Completed, reusable one-second image atlases; no original-media dependency on reads."""

    def __init__(self, cache_root: Path, projects_root: Path | None = None):
        self.projects_root = projects_root
        self.root = Path(cache_root) / PROFILE
        self._directories: dict[str, Path] = {}
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def register_directory(self, cache_key: str, preview_directory: Path) -> None:
        with self._guard:
            self._directories[cache_key] = preview_directory / PROFILE / cache_key

    def directory(self, cache_key: str) -> Path:
        if not isinstance(cache_key, str) or not _KEY.fullmatch(cache_key):
            raise ValueError("Invalid storyboard cache key")
        if cache_key in self._directories:
            return self._directories[cache_key]
        if self.projects_root is not None:
            match = re.fullmatch(r"([0-9a-f]{32})_(p2_[0-9a-f]+)", cache_key)
            if match:
                return self.projects_root / match.group(1) / "preview" / PROFILE / cache_key
        return self.root / cache_key

    @staticmethod
    def _count(duration_ms: int) -> int:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
            raise ValueError("Storyboard duration must be a positive integer")
        return math.ceil(duration_ms / INTERVAL_MS)

    @staticmethod
    def _jpeg_complete(path: Path) -> bool:
        try:
            if path.stat().st_size < 4:
                return False
            with path.open("rb") as handle:
                if handle.read(2) != b"\xff\xd8":
                    return False
                handle.seek(-2, os.SEEK_END)
                return handle.read(2) == b"\xff\xd9"
        except OSError:
            return False

    def read(self, cache_key: str, duration_ms: int | None = None) -> dict | None:
        """Return a complete manifest only; missing/truncated/old-profile caches are absent."""
        directory = self.directory(cache_key)
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                return None
            fields = {"version": 1, "interval_ms": INTERVAL_MS, "tile_width": TILE_WIDTH,
                      "tile_height": TILE_HEIGHT, "columns": COLUMNS, "rows": ROWS}
            if any(type(manifest.get(key)) is not int or manifest[key] != value for key, value in fields.items()):
                return None
            count = manifest.get("frame_count")
            if type(count) is not int or count <= 0:
                return None
            if duration_ms is not None and count != self._count(duration_ms):
                return None
            expected = [f"sheet-{index:05d}.jpg" for index in range(math.ceil(count / FRAMES_PER_SHEET))]
            if manifest.get("sheets") != expected:
                return None
            if not all(self._jpeg_complete(directory / name) for name in expected):
                return None
            return manifest
        except (OSError, ValueError, TypeError):
            return None

    def sheet_path(self, cache_key: str, filename: str) -> Path:
        """Resolve only a published sheet, never a partial artifact or arbitrary local path."""
        if not isinstance(filename, str) or not _SHEET.fullmatch(filename):
            raise HTTPException(404, "预览图不存在。")
        manifest = self.read(cache_key)
        if not manifest or filename not in manifest["sheets"]:
            raise HTTPException(404, "预览图尚未准备完成。")
        return self.directory(cache_key) / filename

    def render(self, source_cached_mp4: Path, cache_key: str, duration_ms: int, ffmpeg: Path,
               update: Callable[[float], None], stopping: threading.Event) -> dict:
        """Build from a playable local preview, then atomically publish its manifest."""
        directory = self.directory(cache_key)
        count = self._count(duration_ms)
        with self._guard:
            lock = self._locks.setdefault(cache_key, threading.Lock())
        with lock:
            ready = self.read(cache_key, duration_ms)
            if ready is not None:
                update(100)
                return ready
            if stopping.is_set():
                raise HTTPException(503, "预览图准备已暂停。")
            directory.mkdir(parents=True, exist_ok=True)
            # A manifest is the commit marker. Incomplete previous attempts cannot be served.
            (directory / "manifest.json").unlink(missing_ok=True)
            temporary = directory / (".partial-" + uuid.uuid4().hex)
            temporary.mkdir()
            expected = [f"sheet-{index:05d}.jpg" for index in range(math.ceil(count / FRAMES_PER_SHEET))]
            manifest = {"version": 1, "interval_ms": INTERVAL_MS, "tile_width": TILE_WIDTH,
                        "tile_height": TILE_HEIGHT, "columns": COLUMNS, "rows": ROWS,
                        "frame_count": count, "sheets": expected}
            # fps is deliberately before scaling: only one decoded frame per second is resized.
            # A one-second cloned tail guarantees the last fractional second has a tile.
            filters = (f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=1,"
                       f"fps=1:start_time=0:round=up,trim=end_frame={count},"
                       f"scale={TILE_WIDTH}:{TILE_HEIGHT}:force_original_aspect_ratio=decrease:reset_sar=1,"
                       f"pad={TILE_WIDTH}:{TILE_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
                       f"tile={COLUMNS}x{ROWS}:nb_frames={FRAMES_PER_SHEET}:padding=0:margin=0:color=black")
            keyframes_only = can_sample_keyframes(source_cached_mp4, duration_ms, ffmpeg, stopping)
            decoder_options = ["-skip_frame", "nokey"] if keyframes_only else []
            args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-threads", str(min(8, os.cpu_count() or 2)), *decoder_options, "-i", str(source_cached_mp4), "-map", "0:v:0", "-an",
                    "-vf", filters, "-fps_mode", "vfr", "-c:v", "mjpeg", "-q:v", "5",
                    "-threads", "1", "-filter_threads", "1", "-start_number", "0",
                    "-progress", "pipe:1", "-nostats", str(temporary / "sheet-%05d.jpg")]
            try:
                run_ffmpeg(args, duration_ms, directory / "storyboard.ffmpeg.log",
                           lambda progress: update(min(99, max(0, progress))), stopping)
                if stopping.is_set():
                    raise HTTPException(503, "预览图准备已暂停。")
                actual = sorted(path.name for path in temporary.glob("sheet-*.jpg"))
                if actual != expected or not all(self._jpeg_complete(temporary / name) for name in expected):
                    raise HTTPException(422, "预览图生成不完整，请重试准备。")
                for name in expected:
                    os.replace(temporary / name, directory / name)
                marker = temporary / "manifest.json"
                marker.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                os.replace(marker, directory / "manifest.json")
                update(100)
                return manifest
            finally:
                # Verify the exact cleanup target remains inside this cache entry.
                resolved = temporary.resolve()
                if resolved.parent == directory.resolve() and resolved.name.startswith(".partial-"):
                    shutil.rmtree(resolved, ignore_errors=True)
