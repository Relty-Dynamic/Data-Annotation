from __future__ import annotations

import copy
import errno
import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.service import ProjectService, empty_annotations, selected_recording_layout, validate_annotations

ROOT = Path(__file__).resolve().parents[1]
MEDIA = b"0000ftyp" + bytes(range(256)) * 512
FAST = b"0000ftyp" + b"fast-local-preview" * 8192
THUMB = b"\xff\xd8thumbnail\xff\xd9"
SHEET = b"\xff\xd8storyboard-sheet\xff\xd9"


class PlaybackSessionTests(unittest.TestCase):
    """Compact playback must be complete locally and never fall back to the NAS."""

    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="playback-session-test-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.app_root = self.root / "app"
        self.app = create_app(self.app_root)
        self.service = self.app.state.service
        self.services = [self.service]
        self.source = self.root / "card"
        self.source.mkdir()
        self.service.probe = self.probe
        self.service.tool = lambda name: Path("fake-" + name + ".exe")
        paths = []
        for index in range(2):
            path = self.source / f"A09999_202609171200{index:02d}_{index:04d}.avi"
            path.write_bytes(b"unchanged-original-" + str(index).encode())
            paths.append(path)
        opened = self.service.open_files([str(path) for path in paths])
        self.project = self.service.load(opened["id"])
        self.ident = self.project["id"]
        self.specs = {}
        for video in self.project["videos"]:
            spec = self.service.preview_spec(self.project, video["id"])
            self.specs[video["id"]] = spec
            spec.target.write_bytes(MEDIA)
            spec.thumbnail_target.write_bytes(THUMB)
            self.service.fast_preview_path(spec).write_bytes(FAST)
            directory = self.service.storyboards.directory(spec.key)
            directory.mkdir(parents=True)
            (directory / "sheet-00000.jpg").write_bytes(SHEET)
            (directory / "manifest.json").write_text(json.dumps({
                "version": 1, "interval_ms": 1000, "tile_width": 160, "tile_height": 90,
                "columns": 10, "rows": 10, "frame_count": 1, "sheets": ["sheet-00000.jpg"],
            }), encoding="utf-8")
        self.encodes = Counter()
        self.encoder_hook = None

    def tearDown(self):
        for service in reversed(self.services):
            service.sessions.close()
            service.previews.close()
        self.assertEqual(self.root.resolve().parent, (ROOT / ".tmp").resolve())
        self.temp.cleanup()

    @staticmethod
    def probe(path):
        return {"duration_ms": 50 if "fast" in path.name else 1000,
                "codec": "mjpeg" if path.suffix == ".avi" else "h264",
                "pixel_format": "yuv420p", "audio_codecs": [],
                "media_start_seconds": 0, "format_start_seconds": 0}

    def encode(self, args, duration_ms, log_path, update, stopping, **kwargs):
        source = str(args[args.index("-i") + 1])
        self.encodes[source] += 1
        if self.encoder_hook:
            self.encoder_hook(args, stopping)
        if stopping.is_set():
            raise HTTPException(503, "Fixture encode cancelled")
        target = Path(args[-1])
        self.assertTrue(target.parent.is_dir())
        target.write_bytes(MEDIA)
        update(99)

    def wait_ready(self, service=None, timeout=5):
        service = service or self.service
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = service.sessions.status(self.ident)
            if status["state"] == "ready":
                return status
            if status["state"] in {"error", "failed"} or (status["state"] == "partial" and status.get("failed", 0)):
                logs = []
                directory = service.storage.directory(self.ident) / "playback"
                if directory.is_dir():
                    logs = [path.name + ": " + path.read_text(encoding="utf-8", errors="replace")[-2000:]
                            for path in directory.glob("*.log")]
                self.fail("Preparation failed: " + repr(status) + "\n" + "\n".join(logs))
            time.sleep(.01)
        self.fail("Preparation did not become ready: " + repr(status))

    def prepare(self, service=None):
        service = service or self.service
        with patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            service.sessions.start(self.ident)
            self.wait_ready(service)
        return service.sessions.manifest(self.ident)

    def asset(self, kind, video_id="v0001", index=None, version=None, service=None):
        service = service or self.service
        return service.sessions.asset(self.ident, kind, video_id, index, version=version)

    def no_source_stat(self):
        original = Path.stat
        source = self.source
        def stat(path, *args, **kwargs):
            if path == source or path.is_relative_to(source):
                raise AssertionError("Ready playback touched the original/NAS: " + str(path))
            return original(path, *args, **kwargs)
        return patch.object(Path, "stat", stat)

    def test_ready_requires_every_local_video_fast_preview_thumbnail_and_sheet(self):
        entered, release = threading.Event(), threading.Event()
        second_source = str(self.specs["v0002"].target)
        def pause_second(args, stopping):
            if str(args[args.index("-i") + 1]) == second_source:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Encode fixture was not released")
        self.encoder_hook = pause_second
        with patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            started = time.monotonic()
            self.service.sessions.start(self.ident)
            self.assertLess(time.monotonic() - started, 1)
            try:
                self.assertTrue(entered.wait(3))
                self.assertNotEqual(self.service.sessions.status(self.ident)["state"], "ready")
                self.assertIsNone(self.asset("normal"))
                with self.assertRaises(HTTPException):
                    self.service.sessions.manifest(self.ident)
            finally:
                release.set()
            self.wait_ready()
        manifest = self.service.sessions.manifest(self.ident)
        local = self.service.storage.directory(self.ident) / "playback"
        for video in self.project["videos"]:
            for kind, index in (("normal", None), ("fast", None), ("thumbnail", None), ("sheet", 0)):
                path = self.asset(kind, video["id"], index, manifest["version"])
                self.assertIsInstance(path, Path)
                self.assertTrue(path.is_relative_to(local))
                self.assertTrue(path.is_file())
        self.assertEqual(self.service.load(self.ident)["_sources"], self.project["_sources"])
        self.assertEqual(self.service.load(self.ident)["annotations"], self.project["annotations"])

    def test_restart_reuses_completed_local_cache_without_encoding_when_originals_are_offline(self):
        manifest = self.prepare()
        self.service.sessions.close()
        restarted = ProjectService(self.app_root)
        restarted.probe = self.probe
        self.services.append(restarted)
        with self.no_source_stat(), patch("backend.session_cache.run_ffmpeg") as encode, \
             patch.object(restarted, "source_available", return_value=False), \
             patch.object(restarted, "preview_spec", side_effect=AssertionError("Unexpected NAS lookup")):
            restarted.sessions.start(self.ident)
            self.wait_ready(restarted)
            self.assertEqual(restarted.sessions.manifest(self.ident)["version"], manifest["version"])
            self.assertEqual(self.asset("normal", version=manifest["version"], service=restarted).read_bytes(), MEDIA)
            encode.assert_not_called()

    def test_ready_assets_and_manifest_work_offline_without_any_nas_stat(self):
        manifest = self.prepare()
        with self.no_source_stat(), patch.object(self.service, "preview_spec", side_effect=AssertionError("NAS lookup")):
            self.assertEqual(self.service.sessions.status(self.ident)["state"], "ready")
            self.assertEqual(self.service.sessions.manifest(self.ident)["version"], manifest["version"])
            for kind, index, expected in (("normal", None, MEDIA), ("fast", None, FAST),
                                          ("thumbnail", None, THUMB), ("sheet", 0, SHEET)):
                self.assertEqual(self.asset(kind, index=index, version=manifest["version"]).read_bytes(), expected)

    def test_reopening_ready_project_keeps_existing_playback_urls_ready(self):
        manifest = self.prepare()
        before = dict(self.encodes)
        with self.no_source_stat(), patch.object(self.service, "preview_spec", side_effect=AssertionError("NAS lookup")):
            for _ in range(3):
                self.assertEqual(self.service.sessions.start(self.ident)["state"], "ready")
                self.assertEqual(self.service.sessions.manifest(self.ident)["version"], manifest["version"])
                self.assertEqual(self.asset("normal", version=manifest["version"]).read_bytes(), MEDIA)
        self.assertEqual(dict(self.encodes), before)

    def test_annotation_saves_preserve_version_but_changed_sources_invalidate_it(self):
        manifest = self.prepare()
        ann = empty_annotations()
        ann["habit"] = [{"id": "water", "label": "喝水", "kind": "point", "start_ms": 250, "end_ms": 250}]
        self.service.update_draft(self.ident, ann, self.project["revision"])
        self.assertEqual(self.service.sessions.status(self.ident)["state"], "ready")
        self.assertEqual(self.service.sessions.manifest(self.ident)["version"], manifest["version"])
        changed = self.service.load(self.ident)
        changed["_sources"]["v0001"]["stamp"]["mtime_ns"] += 1
        self.service.save(changed)
        self.assertNotEqual(self.service.sessions.status(self.ident)["state"], "ready")
        with self.assertRaises(HTTPException):
            self.service.sessions.manifest(self.ident)
        try:
            stale = self.asset("normal", version=manifest["version"])
        except HTTPException as error:
            self.assertEqual(error.status_code, 409)
        else:
            self.assertIsNone(stale)

    def test_missing_local_asset_never_reports_ready_or_falls_back_to_original(self):
        manifest = self.prepare()
        self.asset("fast", version=manifest["version"]).unlink()
        with patch.object(self.service, "media", side_effect=AssertionError("Original fallback")), \
             patch.object(self.service, "preview_spec", side_effect=AssertionError("NAS lookup")):
            self.assertNotEqual(self.service.sessions.status(self.ident)["state"], "ready")
            try:
                missing = self.asset("fast", version=manifest["version"])
            except HTTPException as error:
                self.assertIn(error.status_code, (404, 409))
            else:
                self.assertIsNone(missing)

    def test_strict_version_and_invalid_asset_coordinates_are_rejected(self):
        manifest = self.prepare()
        with self.assertRaises(HTTPException) as caught:
            self.asset("normal", version="obsolete-version")
        self.assertEqual(caught.exception.status_code, 409)
        for kind, video, index in (("normal", "../../source", None), ("sheet", "v0001", -1),
                                    ("sheet", "v0001", 999), ("unknown", "v0001", None)):
            with self.subTest(kind=kind, video=video, index=index):
                try:
                    result = self.asset(kind, video, index, manifest["version"])
                except HTTPException as error:
                    self.assertIn(error.status_code, (404, 409, 422))
                else:
                    self.assertIsNone(result)

    def test_local_cleanup_cancels_encoder_and_never_recreates_playback(self):
        entered, exited = threading.Event(), threading.Event()
        def await_cancel(args, stopping):
            entered.set()
            try:
                if not stopping.wait(5):
                    raise AssertionError("Deletion failed to cancel compact encoder")
            finally:
                exited.set()
        self.encoder_hook = await_cancel
        directory = self.service.storage.directory(self.ident)
        with patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            self.service.sessions.start(self.ident)
            self.assertTrue(entered.wait(3))
            self.service.clear_local_previews(self.ident, self.project["revision"])
            self.assertTrue(exited.wait(2))
        self.assertFalse((directory / "playback").exists())
        self.service.sessions.close()
        self.assertFalse((directory / "playback").exists())
        self.assertTrue(all(Path(item["path"]).is_file() for item in self.project["_sources"].values()))
        self.assertEqual(self.service.load(self.ident)["annotations"], self.project["annotations"])
        self.assertEqual(self.service.sessions.status(self.ident)["state"], "idle")

    def test_replaced_pending_project_can_be_started_again(self):
        paths = [item["path"] for item in self.project["_sources"].values()]
        second = self.service.open_files(paths)["id"]
        third = self.service.open_files(paths)["id"]
        entered, release = threading.Event(), threading.Event()
        prepared = []
        manager = self.service.sessions
        def prepare(job):
            ident = job["project"]["id"]
            if ident == self.ident:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("First project worker was not released")
                manager._check(job)
            prepared.append(ident)
            with manager._condition:
                job["status"].update(state="ready", progress=100)
        with patch.object(manager, "_prepare", side_effect=prepare):
            manager.start(self.ident)
            self.assertTrue(entered.wait(3))
            try:
                manager.start(second)
                manager.start(third)
                self.assertEqual(manager.status(second)["state"], "idle")
                restarted = manager.start(second)
                self.assertEqual(restarted["state"], "running")
            finally:
                release.set()
            deadline = time.monotonic() + 3
            while manager.status(second)["state"] != "ready" and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(manager.status(second)["state"], "ready")
        self.assertEqual(prepared, [second])
        first = manager.status(self.ident)
        self.assertEqual(first["state"], "idle")
        self.assertEqual(first["running"], 0)
        self.assertEqual(first["failed"], 0)

    def test_repair_only_copies_missing_local_sheet_and_keeps_other_assets(self):
        manifest = self.prepare()
        unchanged = {(kind, video["id"]): self.asset(kind, video["id"]).stat().st_mtime_ns
                     for video in self.project["videos"] for kind in ("normal", "fast", "thumbnail")}
        missing = self.asset("sheet", "v0001", 0)
        missing.unlink()
        self.assertEqual(self.service.sessions.status(self.ident)["state"], "partial")
        copy_asset = self.service.sessions._copy_asset
        before = dict(self.encodes)
        with patch.object(self.service.sessions, "_copy_asset", wraps=copy_asset) as copied:
            self.prepare()
        self.assertEqual(copied.call_count, 1)
        self.assertEqual(copied.call_args.args[1], missing)
        self.assertEqual(missing.read_bytes(), SHEET)
        self.assertEqual(dict(self.encodes), before)
        for (kind, video_id), mtime in unchanged.items():
            self.assertEqual(self.asset(kind, video_id).stat().st_mtime_ns, mtime)
        self.assertNotEqual(self.service.sessions.manifest(self.ident)["version"], manifest["version"])

    def test_supplement_reuses_existing_compacts_and_only_prepares_new_video(self):
        self.prepare()
        existing = {video["id"]: (self.asset("normal", video["id"]), self.asset("normal", video["id"]).stat().st_mtime_ns)
                    for video in self.project["videos"]}
        before = dict(self.encodes)
        added = self.source / "A09999_20260917115959_0099.avi"
        added.write_bytes(b"additional-original")
        self.service.supplement(self.ident, [added], self.project["revision"])
        project = self.service.load(self.ident)
        video = next(video for video in project["videos"] if video["name"] == added.name)
        spec = self.service.preview_spec(project, video["id"])
        spec.target.write_bytes(MEDIA)
        spec.thumbnail_target.write_bytes(THUMB)
        self.service.fast_preview_path(spec).write_bytes(FAST)
        directory = self.service.storyboards.directory(spec.key)
        directory.mkdir(parents=True)
        (directory / "sheet-00000.jpg").write_bytes(SHEET)
        (directory / "manifest.json").write_text(json.dumps({
            "version": 1, "interval_ms": 1000, "tile_width": 160, "tile_height": 90,
            "columns": 10, "rows": 10, "frame_count": 1, "sheets": ["sheet-00000.jpg"],
        }), encoding="utf-8")
        self.prepare()
        for video_id, (path, mtime) in existing.items():
            self.assertEqual(self.asset("normal", video_id), path)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
        for source, count in before.items():
            self.assertEqual(self.encodes[source], count)
        self.assertEqual(self.encodes[str(spec.target)], 1)
        self.assertEqual(self.service.sessions.status(self.ident)["ready"], 3)

    def test_invalidate_retry_still_waits_for_writer_after_first_timeout(self):
        entered, release = threading.Event(), threading.Event()
        def hold_writer(args, stopping):
            Path(args[-1]).write_bytes(b"unfinished-compact")
            entered.set()
            if not release.wait(5):
                raise AssertionError("Writer release was not signalled")
        self.encoder_hook = hold_writer
        manager = self.service.sessions
        with patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            manager.start(self.ident)
            self.assertTrue(entered.wait(3))
            job = manager._current
            try:
                with patch.object(job["done"], "wait", return_value=False):
                    with self.assertRaises(HTTPException) as caught:
                        manager.invalidate(self.ident)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertIs(manager._current, job)
                waiting = threading.Event()
                original_wait = job["done"].wait
                def observe_wait(timeout=None):
                    waiting.set()
                    return original_wait(timeout)
                with ThreadPoolExecutor(max_workers=1) as pool, patch.object(job["done"], "wait", side_effect=observe_wait):
                    retried = pool.submit(manager.invalidate, self.ident)
                    self.assertTrue(waiting.wait(2))
                    self.assertFalse(retried.done())
                    release.set()
                    retried.result(timeout=3)
            finally:
                release.set()
            self.assertTrue(job["done"].is_set())
            self.assertEqual(job["status"]["state"], "idle")
            self.assertEqual(job["status"]["failed"], 0)
            local = self.service.storage.directory(self.ident) / "playback"
            self.assertFalse(list(local.glob("*.partial.mp4")))
            self.encoder_hook = None
            manager.start(self.ident)
            self.wait_ready()

    def add_fixture_video(self):
        path = self.source / "A09999_20260917120002_0002.avi"
        path.write_bytes(b"third-original")
        self.service.supplement(self.ident, [path], self.service.load(self.ident)["revision"])
        self.project = self.service.load(self.ident)
        video = next(video for video in self.project["videos"] if video["name"] == path.name)
        spec = self.service.preview_spec(self.project, video["id"])
        self.specs[video["id"]] = spec
        spec.target.write_bytes(MEDIA)
        spec.thumbnail_target.write_bytes(THUMB)
        self.service.fast_preview_path(spec).write_bytes(FAST)
        directory = self.service.storyboards.directory(spec.key)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sheet-00000.jpg").write_bytes(SHEET)
        (directory / "manifest.json").write_text(json.dumps({
            "version": 1, "interval_ms": 1000, "tile_width": 160, "tile_height": 90,
            "columns": 10, "rows": 10, "frame_count": 1, "sheets": ["sheet-00000.jpg"],
        }), encoding="utf-8")

    def test_two_writers_are_bounded_and_out_of_order_completion_preserves_manifest_order(self):
        self.add_fixture_video()
        ids = [video["id"] for video in self.project["videos"]]
        sources = {str(spec.target): video_id for video_id, spec in self.specs.items()}
        entered = {video_id: threading.Event() for video_id in ids}
        release = {video_id: threading.Event() for video_id in ids}
        lock, active, peaks = threading.Lock(), set(), []
        def hold(args, stopping):
            video_id = sources[str(args[args.index("-i") + 1])]
            with lock:
                active.add(video_id)
                peaks.append(len(active))
            entered[video_id].set()
            try:
                if not release[video_id].wait(5):
                    raise AssertionError("Bounded writer fixture was not released")
            finally:
                with lock:
                    active.remove(video_id)
        self.encoder_hook = hold
        manager = self.service.sessions
        with patch("backend.session_cache.os.cpu_count", return_value=8), \
             patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            manager.start(self.ident)
            try:
                self.assertTrue(entered[ids[0]].wait(3))
                self.assertTrue(entered[ids[1]].wait(3))
                self.assertFalse(entered[ids[2]].is_set())
                self.assertEqual(manager.status(self.ident)["running"], 2)
                release[ids[1]].set()
                self.assertTrue(entered[ids[2]].wait(3))
                self.assertFalse(release[ids[0]].is_set())
                release[ids[2]].set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    items = {item["video_id"]: item for item in manager.status(self.ident)["items"]}
                    if items[ids[2]]["state"] == "ready":
                        break
                    time.sleep(.01)
                self.assertEqual(items[ids[2]]["state"], "ready")
                self.assertNotEqual(manager.status(self.ident)["state"], "ready")
                release[ids[0]].set()
                self.wait_ready()
            finally:
                for event in release.values():
                    event.set()
        self.assertEqual(max(peaks), 2)
        self.assertEqual(active, set())
        self.assertEqual([video["id"] for video in manager.manifest(self.ident)["videos"]], ids)
        self.assertEqual(manager.status(self.ident)["ready"], 3)

    def test_clip_failure_keeps_peer_running_and_starts_the_next_queued_clip(self):
        self.add_fixture_video()
        ids = [video["id"] for video in self.project["videos"]]
        sources = {str(spec.target): video_id for video_id, spec in self.specs.items()}
        entered = {video_id: threading.Event() for video_id in ids}
        release = {video_id: threading.Event() for video_id in ids}
        cancelled = threading.Event()
        def hold(args, stopping):
            video_id = sources[str(args[args.index("-i") + 1])]
            entered[video_id].set()
            if not release[video_id].wait(5):
                raise AssertionError("Independent clip fixture was not released")
            if stopping.is_set():
                cancelled.set()
            if video_id == ids[1]:
                raise HTTPException(422, "Second clip is invalid")
        self.encoder_hook = hold
        manager = self.service.sessions
        with patch("backend.session_cache.os.cpu_count", return_value=8), \
             patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            manager.start(self.ident)
            try:
                self.assertTrue(entered[ids[0]].wait(3))
                self.assertTrue(entered[ids[1]].wait(3))
                self.assertFalse(entered[ids[2]].is_set())
                job = manager._entries[self.ident]
                release[ids[1]].set()
                self.assertTrue(entered[ids[2]].wait(3), "Failed clip prevented the next queued clip from starting")
                self.assertFalse(job["stop"].is_set())
                self.assertFalse(job["done"].is_set())
                self.assertFalse(release[ids[0]].is_set())
                status = manager.status(self.ident)
                self.assertEqual((status["state"], status["failed"], status["running"]), ("running", 1, 2))
                release[ids[2]].set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    items = {item["video_id"]: item for item in manager.status(self.ident)["items"]}
                    if items[ids[2]]["state"] == "ready":
                        break
                    time.sleep(.01)
                self.assertEqual(items[ids[2]]["state"], "ready")
                self.assertEqual(items[ids[0]]["state"], "running")
                release[ids[0]].set()
                self.assertTrue(job["done"].wait(3))
            finally:
                for event in release.values():
                    event.set()
        status = manager.status(self.ident)
        items = {item["video_id"]: item for item in status["items"]}
        self.assertEqual((status["state"], status["ready"], status["failed"], status["running"], status["queued"]),
                         ("partial", 2, 1, 0, 0))
        self.assertFalse(cancelled.is_set(), "A clip-local failure cancelled a healthy peer")
        self.assertEqual(items["v0002"]["state"], "error")
        self.assertIn("Second clip is invalid", items["v0002"].get("detail", ""))
        self.assertEqual(items["v0001"]["state"], "ready")
        self.assertEqual(items["v0003"]["state"], "ready")
        local = self.service.storage.directory(self.ident) / "playback"
        self.assertFalse(list(local.glob("*.partial.mp4")))
        self.assertFalse((local / "manifest.json").exists())
        with self.assertRaises(HTTPException):
            manager.manifest(self.ident)

    def test_single_writer_continues_after_failure_and_retry_reuses_the_successful_clip(self):
        first_source = str(self.specs["v0001"].target)
        second_source = str(self.specs["v0002"].target)
        def fail_first(args, stopping):
            if str(args[args.index("-i") + 1]) == first_source:
                raise HTTPException(422, "First clip cannot be decoded")
        self.encoder_hook = fail_first
        manager = self.service.sessions
        with patch("backend.session_cache.os.cpu_count", return_value=2), \
             patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            manager.start(self.ident)
            self.assertTrue(manager._entries[self.ident]["done"].wait(3))
            status = manager.status(self.ident)
            self.assertEqual((status["state"], status["ready"], status["failed"], status["queued"]),
                             ("partial", 1, 1, 0))
            self.assertEqual({item["video_id"]: item["state"] for item in status["items"]},
                             {"v0001": "error", "v0002": "ready"})
            self.assertEqual(self.encodes[first_source], 1)
            self.assertEqual(self.encodes[second_source], 1)
            local = self.service.storage.directory(self.ident) / "playback"
            key = manager._source_key(self.project["_sources"]["v0002"])
            completed = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in local.glob(key + "*")}
            self.assertTrue(completed)
            with self.assertRaises(HTTPException):
                manager.manifest(self.ident)
            self.encoder_hook = None
            manager.start(self.ident, retry_failed=True)
            self.wait_ready()
        self.assertEqual(self.encodes[first_source], 2)
        self.assertEqual(self.encodes[second_source], 1)
        for path, (mtime, contents) in completed.items():
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertEqual(path.read_bytes(), contents)
        self.assertEqual(manager.status(self.ident)["failed"], 0)
        self.assertEqual([video["id"] for video in manager.manifest(self.ident)["videos"]], ["v0001", "v0002"])

    def test_multiple_clip_failures_retain_each_detail_and_other_clips_finish(self):
        self.add_fixture_video()
        failures = {str(self.specs["v0001"].target): (422, "First source contains invalid frames"),
                    str(self.specs["v0002"].target): (503, "Second source connection remained unavailable")}
        def fail_independently(args, stopping):
            error = failures.get(str(args[args.index("-i") + 1]))
            if error:
                raise HTTPException(*error)
        self.encoder_hook = fail_independently
        manager = self.service.sessions
        with patch("backend.session_cache.os.cpu_count", return_value=8), \
             patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            manager.start(self.ident)
            self.assertTrue(manager._entries[self.ident]["done"].wait(3))
        status = manager.status(self.ident)
        items = {item["video_id"]: item for item in status["items"]}
        self.assertEqual((status["state"], status["ready"], status["failed"], status["running"], status["queued"]),
                         ("partial", 1, 2, 0, 0))
        for video_id in ("v0001", "v0002"):
            self.assertEqual(items[video_id]["state"], "error")
            self.assertEqual(items[video_id]["detail"], failures[str(self.specs[video_id].target)][1])
        self.assertEqual(items["v0003"]["state"], "ready")
        with self.assertRaises(HTTPException):
            manager.manifest(self.ident)

    def test_global_resource_failures_cancel_peers_and_wait_for_writers_before_done(self):
        self.add_fixture_video()
        second_source = str(self.specs["v0002"].target)
        third_source = str(self.specs["v0003"].target)
        failures = [HTTPException(507, "Fixture disk budget exhausted"),
                    OSError(errno.ENOSPC, "Fixture disk full"),
                    OSError(getattr(errno, "EDQUOT", 122), "Fixture disk quota exhausted"),
                    OSError(errno.ENOMEM, "Fixture allocation failed"),
                    MemoryError("Fixture memory exhausted")]
        manager = self.service.sessions
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, code=getattr(failure, "errno", None)):
                entered, cancelled, release_cleanup = threading.Event(), threading.Event(), threading.Event()
                def fail_globally(args, stopping):
                    source = str(args[args.index("-i") + 1])
                    if source == second_source:
                        if not entered.wait(3):
                            raise AssertionError("Resource fixture did not have an active peer")
                        raise failure
                    if source == third_source:
                        raise AssertionError("A global resource failure incorrectly started another queued clip")
                    entered.set()
                    if not stopping.wait(5):
                        raise AssertionError("Resource failure did not cancel the active peer")
                    cancelled.set()
                    if not release_cleanup.wait(5):
                        raise AssertionError("Resource cleanup fixture was not released")
                self.encoder_hook = fail_globally
                with patch("backend.session_cache.os.cpu_count", return_value=8), \
                     patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
                    manager.start(self.ident, retry_failed=True)
                    job = manager._entries[self.ident]
                    try:
                        self.assertTrue(entered.wait(3))
                        self.assertTrue(cancelled.wait(3))
                        self.assertFalse(job["done"].is_set())
                        self.assertFalse(job["stop"].is_set())
                        release_cleanup.set()
                        self.assertTrue(job["done"].wait(3))
                    finally:
                        release_cleanup.set()
                status = manager.status(self.ident)
                items = {item["video_id"]: item for item in status["items"]}
                self.assertEqual((status["state"], status["failed"], status["running"]), ("partial", 1, 0))
                self.assertEqual(items["v0002"]["state"], "error")
                self.assertNotEqual(items["v0001"]["state"], "error")
                self.assertEqual(self.encodes[third_source], 0)
                with self.assertRaises(HTTPException):
                    manager.manifest(self.ident)

    def test_top_level_fatal_error_does_not_overwrite_an_earlier_clip_failure(self):
        manager = self.service.sessions
        original_detail = "First clip has a distinct invalid timestamp"
        def fail_after_clip_error(job, directory):
            with manager._condition:
                first, second = job["status"]["items"]
                first.update(state="error", detail=original_detail, progress=0)
                second.update(state="running", detail="Preparing second clip", progress=30)
                manager._counts(job)
            raise HTTPException(507, "Shared filesystem is now full")
        with patch.object(manager, "_prepare_videos", side_effect=fail_after_clip_error):
            manager.start(self.ident)
            self.assertTrue(manager._entries[self.ident]["done"].wait(3))
        status = manager.status(self.ident)
        items = {item["video_id"]: item for item in status["items"]}
        self.assertEqual(items["v0001"]["detail"], original_detail)
        self.assertEqual(items["v0001"]["state"], "error")
        self.assertEqual(status["detail"], "Shared filesystem is now full")
        self.assertEqual(items["v0002"]["state"], "queued")
        self.assertEqual((status["state"], status["failed"], status["running"]), ("partial", 1, 0))
        with self.assertRaises(HTTPException):
            manager.manifest(self.ident)

    def test_local_cleanup_waits_for_both_parallel_writers(self):
        entered = [threading.Event(), threading.Event()]
        cancelled = [threading.Event(), threading.Event()]
        cleanup = [threading.Event(), threading.Event()]
        exited = [threading.Event(), threading.Event()]
        second_source = str(self.specs["v0002"].target)
        def hold(args, stopping):
            index = int(str(args[args.index("-i") + 1]) == second_source)
            Path(args[-1]).write_bytes(b"unfinished-writer")
            entered[index].set()
            try:
                if not stopping.wait(5):
                    raise AssertionError("Delete failed to cancel a parallel writer")
                cancelled[index].set()
                if not cleanup[index].wait(5):
                    raise AssertionError("Delete cleanup fixture was not released")
            finally:
                exited[index].set()
        self.encoder_hook = hold
        manager = self.service.sessions
        directory = self.service.storage.directory(self.ident)
        with patch("backend.session_cache.os.cpu_count", return_value=8), \
             patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            manager.start(self.ident)
            try:
                self.assertTrue(all(event.wait(3) for event in entered))
                job = manager._current
                with ThreadPoolExecutor(max_workers=1) as pool:
                    deletion = pool.submit(self.service.clear_local_previews, self.ident, self.project["revision"])
                    try:
                        self.assertTrue(all(event.wait(3) for event in cancelled))
                        self.assertFalse(deletion.done())
                        self.assertTrue(directory.exists())
                        self.assertFalse(job["done"].is_set())
                        cleanup[0].set()
                        self.assertTrue(exited[0].wait(3))
                        self.assertFalse(deletion.done())
                        self.assertFalse(job["done"].is_set())
                    finally:
                        for event in cleanup:
                            event.set()
                    deletion.result(timeout=3)
            finally:
                for event in cleanup:
                    event.set()
        self.assertTrue(all(event.is_set() for event in exited))
        self.assertTrue(job["done"].is_set())
        self.assertFalse((directory / "playback").exists())
        self.assertEqual(self.service.load(self.ident)["annotations"], self.project["annotations"])
        self.assertTrue(all(Path(source["path"]).is_file() for source in self.project["_sources"].values()))

    def test_concurrent_disk_budget_rejects_second_writer_before_creating_output(self):
        from backend.session_cache import RESERVE_BYTES
        entered = threading.Event()
        source_available = self.service.source_available
        second_path = Path(self.project["_sources"]["v0002"]["path"])
        def available(path, source):
            if path == second_path and not entered.wait(3):
                raise AssertionError("First writer did not acquire its disk reservation")
            return source_available(path, source)
        def hold(args, stopping):
            entered.set()
            if not stopping.wait(5):
                raise AssertionError("Disk-space failure did not stop the active writer")
        self.encoder_hook = hold
        manager = self.service.sessions
        # Enough for either one-second clip and its images, not both outstanding sets.
        available_space = SimpleNamespace(free=RESERVE_BYTES + 1536 * 1024)
        with patch("backend.session_cache.os.cpu_count", return_value=8), \
             patch("backend.session_cache.shutil.disk_usage", return_value=available_space), \
             patch.object(self.service, "source_available", side_effect=available), \
             patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            manager.start(self.ident)
            job = manager._entries[self.ident]
            self.assertTrue(entered.wait(3))
            self.assertTrue(job["done"].wait(3))
        status = manager.status(self.ident)
        items = {item["video_id"]: item for item in status["items"]}
        self.assertEqual((status["state"], status["failed"], status["running"]), ("partial", 1, 0))
        self.assertEqual(items["v0002"]["state"], "error")
        self.assertIn("磁盘空间不足", items["v0002"]["detail"])
        self.assertEqual(self.encodes[str(self.specs["v0001"].target)], 1)
        self.assertEqual(self.encodes[str(self.specs["v0002"].target)], 0)
        local = self.service.storage.directory(self.ident) / "playback"
        key = manager._source_key(self.project["_sources"]["v0002"])
        self.assertFalse((local / (key + ".compact-v1.mp4")).exists())
        self.assertFalse(list(local.glob("*.partial.mp4")))

    def test_duplicate_source_keys_share_one_writer_and_completed_local_assets(self):
        changed = self.service.load(self.ident)
        changed["_sources"]["v0002"] = copy.deepcopy(changed["_sources"]["v0001"])
        self.service.save(changed)
        self.project = self.service.load(self.ident)
        entered, release = threading.Event(), threading.Event()
        def hold(args, stopping):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Duplicate-source fixture was not released")
        self.encoder_hook = hold
        manager = self.service.sessions
        with patch("backend.session_cache.os.cpu_count", return_value=8), \
             patch("backend.session_cache.run_ffmpeg", side_effect=self.encode):
            manager.start(self.ident)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(manager.status(self.ident)["running"], 1)
                release.set()
                self.wait_ready()
            finally:
                release.set()
        self.assertEqual(sum(self.encodes.values()), 1)
        self.assertEqual(self.asset("normal", "v0001"), self.asset("normal", "v0002"))
        self.assertEqual(manager.status(self.ident)["ready"], 2)

    def remove_fixture_source_previews(self):
        for spec in self.specs.values():
            for path in (spec.target, spec.thumbnail_target, self.service.fast_preview_path(spec),
                         self.service.storyboards.directory(spec.key) / "sheet-00000.jpg",
                         self.service.storyboards.directory(spec.key) / "manifest.json"):
                path.unlink()

    def fake_storyboard(self, manager, normal, key, duration, ffmpeg, update, stopping):
        self.assertTrue(normal.is_relative_to(self.app_root / ".local" / "projects"))
        directory = manager._local_storyboards.directory(key)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sheet-00000.jpg").write_bytes(SHEET)
        manifest = {**manager._storyboard_metadata(duration), "sheets": ["sheet-00000.jpg"]}
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        update(100)
        return manifest

    def test_new_project_encodes_raw_once_without_generating_source_previews(self):
        self.remove_fixture_source_previews()
        originals = {Path(source["path"]): Path(source["path"]).read_bytes() for source in self.project["_sources"].values()}
        def fast(service, normal, target, duration, update, stopping):
            self.assertTrue(normal.is_relative_to(self.app_root / ".local" / "projects"))
            target.write_bytes(FAST)
            update(100)
        def cover(service, normal, target, duration, stopping):
            self.assertTrue(normal.is_relative_to(self.app_root / ".local" / "projects"))
            target.write_bytes(THUMB)
        manager = self.service.sessions
        with patch("backend.session_cache.run_ffmpeg", side_effect=self.encode), \
             patch("backend.session_cache.render_local_fast", side_effect=fast), \
             patch("backend.session_cache.render_local_thumbnail", side_effect=cover), \
             patch.object(manager._local_storyboards, "render", side_effect=lambda *args: self.fake_storyboard(manager, *args)), \
             patch.object(self.service.previews, "request_batch", side_effect=AssertionError("Unexpected full NAS transcode")):
            manager.start(self.ident)
            self.wait_ready()
        for path, payload in originals.items():
            self.assertEqual(self.encodes[str(path)], 1)
            self.assertEqual(path.read_bytes(), payload)
        self.assertTrue(all(not spec.target.exists() for spec in self.specs.values()))
        self.assertTrue(all(not self.service.fast_preview_path(spec).exists() for spec in self.specs.values()))
        self.assertEqual(manager.status(self.ident)["operation"], "assets")

    def test_cancelled_derivatives_resume_offline_without_reencoding_finished_normal_or_fast(self):
        self.remove_fixture_source_previews()
        manager = self.service.sessions
        entered = threading.Event()
        fast_calls = Counter()
        first_fast_ready = threading.Event()
        second_key = manager._source_key(self.project["_sources"]["v0002"])
        def fast(service, normal, target, duration, update, stopping):
            fast_calls[str(normal)] += 1
            target.write_bytes(FAST)
            if not normal.name.startswith(second_key):
                first_fast_ready.set()
            update(100)
        def cover(service, normal, target, duration, stopping):
            if normal.name.startswith(second_key):
                if not first_fast_ready.wait(3):
                    raise AssertionError("First clip fast preview did not finish")
                entered.set()
                if not stopping.wait(5):
                    raise AssertionError("Cancel was not received")
                raise HTTPException(503, "Fixture derivative cancelled")
            target.write_bytes(THUMB)
        with patch("backend.session_cache.run_ffmpeg", side_effect=self.encode), \
             patch("backend.session_cache.render_local_fast", side_effect=fast), \
             patch("backend.session_cache.render_local_thumbnail", side_effect=cover), \
             patch.object(manager._local_storyboards, "render", side_effect=lambda *args: self.fake_storyboard(manager, *args)):
            manager.start(self.ident)
            self.assertTrue(entered.wait(3))
            manager.invalidate(self.ident)
        before = dict(self.encodes)
        before_fast = dict(fast_calls)
        manager.close()
        restarted = ProjectService(self.app_root)
        restarted.probe = self.probe
        restarted.tool = self.service.tool
        self.services.append(restarted)
        with self.no_source_stat(), patch.object(restarted, "source_available", return_value=False), \
             patch.object(restarted, "preview_spec", side_effect=AssertionError("Unexpected NAS lookup")), \
             patch("backend.session_cache.run_ffmpeg", side_effect=AssertionError("Unexpected normal encode")), \
             patch("backend.session_cache.render_local_fast", side_effect=fast), \
             patch("backend.session_cache.render_local_thumbnail", side_effect=lambda service, normal, target, duration, stopping: target.write_bytes(THUMB)), \
             patch.object(restarted.sessions._local_storyboards, "render", side_effect=lambda *args: self.fake_storyboard(restarted.sessions, *args)):
            restarted.sessions.start(self.ident)
            self.wait_ready(restarted)
        self.assertEqual(dict(self.encodes), before)
        self.assertEqual(dict(fast_calls), before_fast)
        self.assertFalse(list((restarted.storage.directory(self.ident) / "playback").glob("*.progress.json")))

    def test_raw_source_changed_during_encode_is_not_published(self):
        self.remove_fixture_source_previews()
        first = Path(self.project["_sources"]["v0001"]["path"])
        manager = self.service.sessions
        first_key = manager._source_key(self.project["_sources"]["v0001"])
        fast_sources = []
        def replace_source(args, stopping):
            if Path(args[args.index("-i") + 1]) == first:
                first.write_bytes(b"source-was-replaced-during-encode")
        def fast(service, normal, target, duration, update, stopping):
            fast_sources.append(normal.name)
            target.write_bytes(FAST)
            update(100)
        self.encoder_hook = replace_source
        with patch("backend.session_cache.run_ffmpeg", side_effect=self.encode), \
             patch("backend.session_cache.render_local_fast", side_effect=fast), \
             patch("backend.session_cache.render_local_thumbnail", side_effect=lambda service, normal, target, duration, stopping: target.write_bytes(THUMB)), \
             patch.object(manager._local_storyboards, "render", side_effect=lambda *args: self.fake_storyboard(manager, *args)):
            manager.start(self.ident)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = manager.status(self.ident)
                if status["state"] == "partial":
                    break
                time.sleep(.01)
            self.assertEqual(status["state"], "partial")
        local = self.service.storage.directory(self.ident) / "playback"
        self.assertFalse((local / (first_key + ".compact-v1.mp4")).exists())
        self.assertFalse(any(name.startswith(first_key) for name in fast_sources))
        self.assertFalse(list(local.glob("*.partial.mp4")))

    def test_real_two_second_compact_preserves_frame_times_and_annotation_coordinates(self):
        tools = ROOT / ".tools" / "ffmpeg" / "bin"
        if not all((tools / (name + ".exe")).is_file() for name in ("ffmpeg", "ffprobe")):
            self.skipTest("Project FFmpeg runtime is unavailable")
        service = ProjectService(self.root / "real-app")
        service.tool = lambda name: tools / (name + ".exe")
        self.services.append(service)
        source = self.root / "real-originals" / "A09999_20260917130000_0000.avi"
        source.parent.mkdir()
        service.command([str(service.tool("ffmpeg")), "-v", "error", "-nostdin", "-y",
                         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
                         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                         "-t", "2", "-c:v", "mjpeg", "-q:v", "4", "-threads", "1",
                         "-c:a", "pcm_s16le", str(source)], timeout=15)
        original = source.read_bytes()
        opened = service.open_files([str(source)])
        project = service.load(opened["id"])
        ann = empty_annotations()
        ann["scene"] = [{"id": "scene", "label": "室内", "kind": "interval", "start_ms": 0, "end_ms": 2000}]
        ann["habit"] = [{"id": "water", "label": "喝水", "kind": "point", "start_ms": 1500, "end_ms": 1500}]
        service.update_draft(project["id"], ann, project["revision"])
        before = service.load(project["id"])
        original_ident = self.ident
        self.ident = project["id"]
        try:
            service.sessions.start(self.ident)
            self.wait_ready(service, timeout=45)
            manifest = service.sessions.manifest(self.ident)
            compact = self.asset("normal", version=manifest["version"], service=service)
            self.assertTrue(compact.is_relative_to(service.storage.directory(self.ident) / "playback"))
            info = service.probe(compact)
            self.assertEqual(info["codec"], "h264")
            self.assertLessEqual(abs(info["duration_ms"] - before["duration_ms"]), 1)
            self.assertLess(abs(info["media_start_seconds"]), .001)
            def frame_times(path):
                probe = service.command([str(service.tool("ffprobe")), "-v", "error", "-select_streams", "v:0",
                                         "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)], timeout=15)
                return [round(float(frame["best_effort_timestamp_time"]) * 1000)
                        for frame in json.loads(probe.stdout)["frames"]]
            self.assertEqual(frame_times(compact), frame_times(source))
            dimensions = json.loads(service.command([str(service.tool("ffprobe")), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", str(compact)]).stdout)["streams"][0]
            self.assertEqual((dimensions["width"], dimensions["height"]), (480, 270))
            after = service.load(self.ident)
            for key in ("videos", "annotations", "revision", "source_fingerprint"):
                self.assertEqual(after[key], before[key])
            self.assertEqual(source.read_bytes(), original)
        finally:
            self.ident = original_ident

    def test_versioned_routes_serve_local_ranges_and_images_without_remote_fallback(self):
        manifest = self.prepare()
        prefix = f"/api/session-media/{self.ident}/{manifest['version']}/v0001"
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            with self.no_source_stat(), \
                 patch.object(self.service, "media", side_effect=AssertionError("Remote media fallback")), \
                 patch.object(self.service, "preview_status", side_effect=AssertionError("Remote status fallback")), \
                 patch.object(self.service, "preview_spec", side_effect=AssertionError("Remote spec fallback")):
                response = client.get(prefix, headers={"Range": "bytes=123-77999"})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, MEDIA[123:78000])
                self.assertEqual(response.headers["Content-Range"], f"bytes 123-77999/{len(MEDIA)}")
                self.assertEqual(response.headers["Cache-Control"], "private, max-age=3600")
                head = client.head(prefix, headers={"Range": "bytes=10-19"})
                self.assertEqual((head.status_code, head.content, head.headers["content-length"]), (206, b"", "10"))
                fast = client.get(prefix + "?fast=true", headers={"Range": "bytes=-20"})
                self.assertEqual((fast.status_code, fast.content), (206, FAST[-20:]))
                for kind, suffix, expected in (("thumbnails", "", THUMB), ("storyboards", "/0", SHEET)):
                    url = f"/api/session-{kind}/{self.ident}/{manifest['version']}/v0001{suffix}"
                    image = client.get(url)
                    self.assertEqual((image.status_code, image.content), (200, expected))
                    self.assertEqual(image.headers["content-type"], "image/jpeg")
                invalid = client.get(f"/api/session-media/{self.ident}/old-version/v0001")
                self.assertEqual(invalid.status_code, 409)
                self.asset("normal", version=manifest["version"]).unlink()
                missing = client.get(prefix)
                self.assertIn(missing.status_code, (404, 409))


class SkipFailedPlaybackTests(unittest.TestCase):
    setUp = PlaybackSessionTests.setUp
    tearDown = PlaybackSessionTests.tearDown
    probe = staticmethod(PlaybackSessionTests.probe)
    encode = PlaybackSessionTests.encode
    prepare = PlaybackSessionTests.prepare
    wait_ready = PlaybackSessionTests.wait_ready
    no_source_stat = PlaybackSessionTests.no_source_stat

    def make_partial(self, failed_id='v0001'):
        def fail(args, stopping):
            if str(args[args.index('-i') + 1]) == str(self.specs[failed_id].target):
                raise HTTPException(422, 'Invalid source duration')
        self.encoder_hook = fail
        with patch('backend.session_cache.run_ffmpeg', side_effect=self.encode):
            self.service.sessions.start(self.ident)
            job = self.service.sessions._entries[self.ident]
            self.assertTrue(job['done'].wait(5))
        self.assertEqual(job['status']['state'], 'partial')
        self.encoder_hook = None

    def test_skip_preserves_sources_and_coordinates_and_survives_restart(self):
        self.make_partial()
        before = self.service.load(self.ident)
        public = self.service.skip_failed_videos(self.ident, ['v0001'], before['revision'])
        after = self.service.load(self.ident)
        self.assertEqual(before['videos'], after['videos'])
        self.assertEqual(before['_sources'], after['_sources'])
        self.assertEqual(before['annotations'], after['annotations'])
        self.assertEqual(public['videos'], before['videos'][1:])
        self.assertEqual(public['gaps'], [{'start_ms': 0, 'end_ms': 1000}])
        self.assertEqual(public['skipped_videos'][0]['id'], 'v0001')
        reopened = ProjectService(self.app_root)
        self.services.append(reopened)
        with patch('backend.session_cache.run_ffmpeg', side_effect=AssertionError('Unexpected encode')):
            reopened.sessions.start(self.ident)
            self.wait_ready(reopened)
        with self.no_source_stat():
            manifest = reopened.sessions.manifest(self.ident)
            self.assertEqual([v['id'] for v in manifest['videos']], ['v0002'])
            self.assertIsNone(reopened.sessions.asset(self.ident, 'normal', 'v0001'))

    def test_skip_requires_explicit_confirmation_exact_failures_and_revision(self):
        self.make_partial()
        with TestClient(self.app, base_url='http://127.0.0.1') as client:
            url = f'/api/projects/{self.ident}/session/skip-failed'
            body = {'video_ids': ['v0001'], 'expected_revision': self.project['revision']}
            self.assertEqual(client.post(url, json=body).status_code, 422)
            self.assertEqual(client.post(url, json={**body, 'confirmed': False}).status_code, 422)
            self.assertEqual(client.post(url, json={**body, 'confirmed': True, 'video_ids': ['v0002']}).status_code, 409)
            self.assertEqual(client.post(url, json={**body, 'confirmed': True, 'expected_revision': 999}).status_code, 409)
            self.assertEqual(client.post(url, json={**body, 'confirmed': True}).status_code, 200)

    def test_skip_does_not_discard_existing_annotations(self):
        self.make_partial()
        project = self.service.load(self.ident)
        project['annotations']['scene'] = [{'id': 'existing', 'label': '室内', 'kind': 'interval', 'start_ms': 0, 'end_ms': 2000}]
        self.service.save(project)
        with self.assertRaises(HTTPException) as raised:
            self.service.skip_failed_videos(self.ident, ['v0001'], project['revision'])
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.service.load(self.ident), project)

    def test_skip_last_export_roundtrip_and_restore(self):
        self.make_partial('v0002')
        public = self.service.skip_failed_videos(self.ident, ['v0002'], self.project['revision'])
        self.assertEqual(public['gaps'], [{'start_ms': 1000, 'end_ms': 2000}])
        annotations = empty_annotations()
        for axis, label in [('scene', '室内'), ('posture', '坐')]:
            annotations[axis] = [{'id': axis, 'label': label, 'kind': 'interval', 'start_ms': 0, 'end_ms': 1000}]
        self.service.update_draft(self.ident, annotations, public['revision'])
        project = self.service.load(self.ident)
        _, documents = self.service.export_documents(project)
        self.assertEqual(json.loads(documents['scene'])['timebase']['skipped_videos'][0]['id'], 'v0002')
        timeline = self.source / 'timeline'
        timeline.mkdir()
        for axis, data in documents.items():
            (timeline / f'{axis}.timeline.json').write_bytes(data)
        fresh = copy.deepcopy(project)
        fresh.pop('_skipped_video_names')
        fresh['source_dir'] = str(self.source)
        imported, _ = self.service.read_external(fresh)
        self.assertEqual(imported, annotations)
        self.assertEqual(fresh['_skipped_video_names'], project['_skipped_video_names'])
        restored = self.service.restore_skipped_videos(self.ident, project['revision'])
        self.assertEqual(restored['skipped_videos'], [])
        self.assertEqual(restored['annotations'], annotations)
        self.assertEqual(restored['videos'], self.project['videos'])
        self.assertEqual(len(self.prepare()['videos']), 2)

    def test_all_failed_running_or_queued_cannot_skip(self):
        self.make_partial()
        job = self.service.sessions._entries[self.ident]
        original = copy.deepcopy(job['status'])
        for state in ['running', 'queued', 'error']:
            job['status'] = copy.deepcopy(original)
            job['status']['items'][1]['state'] = state
            with self.assertRaises(HTTPException):
                self.service.skip_failed_videos(self.ident, ['v0001'], self.project['revision'])
        job['status'] = original
        job['done'].clear()
        try:
            with self.assertRaises(HTTPException):
                self.service.skip_failed_videos(self.ident, ['v0001'], self.project['revision'])
        finally:
            job['done'].set()

    def test_middle_exclusion_keeps_a_real_gap_and_rejects_cross_gap_labels(self):
        videos = [{'id': f'v{index}', 'name': f'A09999_2026091712000{index}_{index:04d}.avi',
                   'start_ms': index * 1000, 'end_ms': (index + 1) * 1000, 'duration_ms': 1000}
                  for index in range(3)]
        project = {'videos': videos, 'duration_ms': 3000, '_skipped_video_names': {videos[1]['name']: 'Failed'}}
        runs, bridges, gaps = selected_recording_layout(project)
        self.assertEqual(bridges, [])
        self.assertEqual(gaps, [{'start_ms': 1000, 'end_ms': 2000}])
        self.assertEqual([run['video_ids'] for run in runs], [['v0'], ['v2']])
        annotations = empty_annotations()
        annotations['scene'] = [{'id': 'cross', 'label': '室内', 'start_ms': 0, 'end_ms': 3000}]
        with self.assertRaises(HTTPException):
            validate_annotations(annotations, 3000, videos=[videos[0], videos[2]])

    def test_supplement_keeps_skipped_source_and_selection(self):
        self.make_partial()
        public = self.service.skip_failed_videos(self.ident, ['v0001'], self.project['revision'])
        path = self.source / 'A09999_20260917120002_0002.avi'
        path.write_bytes(b'new-original')
        result = self.service.supplement(self.ident, [path], public['revision'])
        self.assertEqual(result['added_count'], 1)
        self.assertEqual(len(result['project']['videos']), 2)
        self.assertEqual(result['project']['skipped_videos'][0]['name'], self.project['videos'][0]['name'])
        self.assertEqual(len(self.service.load(self.ident)['videos']), 3)
        self.assertEqual(result['project']['videos'][0]['start_ms'], 1000)


if __name__ == "__main__":
    unittest.main()
