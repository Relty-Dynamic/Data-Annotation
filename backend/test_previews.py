from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.previews import PreviewManager, PreviewSpec, run_ffmpeg
from backend.service import ProjectService

ROOT = Path(__file__).resolve().parents[1]


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Background preview did not reach expected state")


class PreviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="preview-queue-test-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.temp.cleanup()

    def spec(self, key):
        return PreviewSpec(key, self.root / (key + ".avi"), {"duration_ms": 1000}, self.root / (key + ".mp4"), self.root / (key + ".error.json"))

    def test_intranet_does_not_serve_original_as_preview(self):
        original = self.root / "original.mp4"
        original.write_bytes(b"original-video")
        source = {"media_start_seconds": 0, "format_start_seconds": 0,
                  "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []}
        spec = PreviewSpec("original", original, source, self.root / "preview.mp4", self.root / "preview.error.json")
        with patch.object(ProjectService, "source_available", return_value=True):
            with patch.dict(os.environ, {"DATAMARK_ORIGIN": "https://192.168.2.126"}):
                self.assertIsNone(ProjectService.cached_preview_for_spec(spec))
            with patch.dict(os.environ, {"DATAMARK_ORIGIN": ""}):
                self.assertEqual(ProjectService.cached_preview_for_spec(spec), original)

    def manager(self, render):
        manager = PreviewManager(render)
        self.managers.append(manager)
        return manager

    def test_latest_selection_replaces_stale_queue_and_promotes_current_without_parallel_encoders(self):
        release = threading.Event()
        order, active, max_active = [], 0, 0

        def render(spec, update, stopping):
            nonlocal active, max_active
            active += 1
            max_active = max(active, max_active)
            order.append(spec.key)
            if spec.key == "a":
                while not release.wait(0.02):
                    if stopping.is_set():
                        return
            update(42)
            active -= 1

        manager = self.manager(render)
        a, b, c, d, e = [self.spec(key) for key in "abcde"]
        manager.request([a, b, c])
        wait_until(lambda: manager.status(a)["state"] == "running")
        manager.request([c, d, e])
        manager.request([c, d, e])
        self.assertEqual(manager.pending, ["c", "d", "e"])
        self.assertEqual(manager.status(b)["state"], "idle")
        release.set()
        wait_until(lambda: manager.status(e)["state"] == "ready")
        self.assertEqual(order, ["a", "c", "d", "e"])
        self.assertEqual(max_active, 1)

    def test_interactive_promotion_preserves_every_pinned_batch_item(self):
        release = threading.Event()
        order = []

        def render(spec, update, stopping):
            order.append(spec.key)
            if spec.key == "a":
                while not release.wait(0.01):
                    if stopping.is_set():
                        return

        manager = self.manager(render)
        a, b, c, d = [self.spec(key) for key in "abcd"]
        manager.request_batch("project", [a, b, c, d])
        wait_until(lambda: manager.status(a)["state"] == "running")
        manager.request([d])
        self.assertEqual(manager.pending, ["d", "b", "c"])
        manager.request([d])
        self.assertEqual(manager.pending, ["d", "b", "c"])
        release.set()
        wait_until(lambda: manager.status(c)["state"] == "ready")
        self.assertEqual(order, ["a", "d", "b", "c"])

    def test_failure_survives_restart_and_requires_explicit_retry(self):
        calls = []
        spec = self.spec("failed")

        def render(spec, update, stopping):
            calls.append(spec.key)
            if len(calls) == 1:
                raise HTTPException(422, "素材读取失败，重试可恢复。")

        manager = self.manager(render)
        manager.request([spec])
        wait_until(lambda: spec.error_path.is_file())
        self.assertEqual(manager.status(spec)["state"], "error")
        manager.request([spec])
        self.assertEqual(calls, ["failed"])
        manager.close()
        restarted = self.manager(render)
        self.assertEqual(restarted.status(spec)["state"], "error")
        restarted.request([spec])
        self.assertEqual(calls, ["failed"])
        restarted.request([spec], retry_key=spec.key)
        wait_until(lambda: restarted.status(spec)["state"] == "ready")
        self.assertEqual(calls, ["failed", "failed"])
        self.assertFalse(spec.error_path.exists())

    def test_progress_microseconds_and_stall_timeout_are_observable(self):
        progress = []
        script = "print('out_time_us=250000',flush=True); print('out_time_us=500000',flush=True)"
        run_ffmpeg([sys.executable, "-u", "-c", script], 1000, self.root / "progress.log", progress.append, threading.Event(), stall_seconds=2)
        self.assertEqual(progress, [25, 50])
        started = time.monotonic()
        with self.assertRaisesRegex(HTTPException, "没有处理进度"):
            run_ffmpeg([sys.executable, "-u", "-c", "import time; time.sleep(10)"], 1000, self.root / "stalled.log", progress.append, threading.Event(), stall_seconds=0.1)
        self.assertLess(time.monotonic() - started, 3)


class PreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="preview-api-test-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.app = create_app(self.root, auth_required=False)
        self.service = self.app.state.service
        self.service.probe = lambda path: {"duration_ms": 1000, "media_start_seconds": 0, "format_start_seconds": 0, "codec": "mjpeg", "pixel_format": "yuvj420p", "audio_codecs": []}
        self.paths = [self.root / f"A09999_2026091409000{index}_000{index}.avi" for index in range(4)]
        for path in self.paths:
            path.write_bytes(b"synthetic-source")
        self.project = self.service.create(self.paths, "Test preview", None)
        self.service.save(self.project)

    def tearDown(self):
        self.service.previews.close()
        self.temp.cleanup()

    def test_status_and_media_get_never_start_encoder_or_block(self):
        ident = self.project["id"]
        with patch.object(self.service.previews, "render") as render:
            with TestClient(self.app, base_url="http://127.0.0.1") as client:
                response = client.get(f"/api/previews/{ident}/v0001")
                self.assertEqual(response.json()["state"], "idle")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                response = client.get(f"/api/media/{ident}/v0001")
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()["state"], "idle")
                render.assert_not_called()
                self.assertIsNone(self.service.previews.worker)

    def test_ready_media_range_validates_once_without_status_round_trip(self):
        ident = self.project['id']
        with TestClient(self.app, base_url='http://127.0.0.1') as client:
            spec = self.service.preview_spec(self.service.load(ident), 'v0001')
            spec.target.write_bytes(b'finished-preview-data')
            with patch.object(self.service, 'preview_status', side_effect=AssertionError('Repeated remote status lookup')), \
                 patch.object(self.service.source_cache, 'validate', wraps=self.service.source_cache.validate) as validate:
                response = client.get(f'/api/media/{ident}/v0001', headers={'Range': 'bytes=0-7'})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, b'finished')
                validate.assert_called_once()

    def test_post_progress_ready_and_range_are_separate_and_reusable(self):
        release = threading.Event()
        started = threading.Event()
        ident = self.project["id"]
        url = f"/api/previews/{ident}/v0001"
        calls = []

        def render(spec, update, stopping):
            calls.append(spec.key)
            update(37.5)
            started.set()
            while not release.wait(0.02):
                if stopping.is_set():
                    return
            self.complete_batch_item(spec)
            self.complete_fast(spec)
            spec.thumbnail_target.write_bytes(b"\xff\xd8thumbnail\xff\xd9")
            self.complete_storyboard(spec)
            spec.target.write_bytes(b"finished-preview-data")

        self.service.previews.render = render
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            started_at = time.monotonic()
            response = client.post(url, json={"prefetch": False})
            self.assertLess(time.monotonic() - started_at, 2)
            self.assertIn(response.json()["state"], ["queued", "running"])
            self.assertTrue(started.wait(2))
            self.assertEqual(client.get(url).json()["progress"], 37.5)
            self.assertEqual(client.get(f"/api/media/{ident}/v0001").status_code, 202)
            release.set()
            wait_until(lambda: self.service.preview_status(ident, "v0001")["state"] == "ready")
            response = client.get(f"/api/media/{ident}/v0001", headers={"Range": "bytes=0-7"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b"finished")
            client.post(url, json={"prefetch": False})
            self.assertEqual(len(calls), 1)
        restarted = ProjectService(self.root)
        try:
            self.assertEqual(restarted.preview_status(ident, "v0001")["state"], "ready")
        finally:
            restarted.previews.close()

    def test_legacy_cache_migrates_and_new_project_has_private_cache(self):
        ident = self.project["id"]
        legacy = self.service.cache / (self.project["source_fingerprint"] + "_v0002.mp4")
        legacy.write_bytes(b"legacy-finished-proxy")
        spec = self.service.preview_spec(self.project, "v0002")
        spec.thumbnail_target.write_bytes(b"\xff\xd8thumbnail\xff\xd9")
        self.complete_storyboard(spec)
        self.service.migrate_legacy_projects()
        with patch.object(self.service.previews, "render") as render:
            self.assertEqual(self.service.preview_status(ident, "v0002")["state"], "ready")
            self.service.prepare_preview(ident, "v0002", prefetch=False)
            render.assert_not_called()
        subset = self.service.create([self.paths[1]], "Subset", None)
        self.service.save(subset)
        self.assertNotEqual(subset["source_fingerprint"], self.project["source_fingerprint"])
        self.assertNotEqual(self.service.preview_status(subset["id"], "v0001")["state"], "ready")
        self.assertEqual(self.service.media(ident, "v0002").read_bytes(), b"legacy-finished-proxy")
        self.assertFalse(legacy.exists())

    def test_post_only_prefetches_next_two_and_missing_next_does_not_block_current(self):
        recorded = []
        with patch.object(self.service.previews, "request", side_effect=lambda specs, retry_key=None: recorded.append([spec.path for spec in specs])):
            self.service.prepare_preview(self.project["id"], "v0001")
            self.assertEqual(recorded[-1], self.paths[:3])
            self.paths[1].unlink()
            self.service.prepare_preview(self.project["id"], "v0001")
            self.assertEqual(recorded[-1], [self.paths[0], self.paths[2]])

    def test_removed_finished_cache_returns_idle_and_can_regenerate(self):
        calls = []

        def render(spec, update, stopping):
            calls.append(spec.key)
            self.complete_batch_item(spec)

        self.service.previews.render = render
        ident = self.project["id"]
        self.service.prepare_preview(ident, "v0001", prefetch=False)
        spec = self.service.preview_spec(self.project, "v0001")
        wait_until(lambda: self.service.previews.status(spec)["state"] == "ready")
        spec.target.unlink()
        status = self.service.preview_status(ident, "v0001")
        self.assertEqual(status["state"], "idle")
        self.assertIsNone(status["url"])
        self.service.prepare_preview(ident, "v0001", prefetch=False)
        wait_until(lambda: self.service.preview_status(ident, "v0001")["state"] == "ready")
        self.assertEqual(len(calls), 2)

    def test_fast_cache_has_independent_readiness_and_supports_offline_range(self):
        ident = self.project["id"]
        spec = self.service.preview_spec(self.project, "v0001")
        self.complete_batch_item(spec)
        fast = self.service.fast_preview_path(spec)
        fast.unlink()
        self.paths[0].unlink()
        with patch.object(self.service.previews, "request") as request:
            with TestClient(self.app, base_url="http://127.0.0.1") as client:
                self.assertEqual(client.get(f"/api/previews/{ident}/v0001").json()["state"], "ready")
                self.assertEqual(client.get(f"/api/previews/{ident}/v0001?fast=true").json()["state"], "idle")
                self.assertEqual(client.get(f"/api/media/{ident}/v0001?fast=true").status_code, 202)
                self.assertNotEqual(self.service.preparation_status(ident)["items"][0]["state"], "ready")
                request.assert_not_called()
                self.complete_fast(spec)
                status = client.get(f"/api/previews/{ident}/v0001?fast=true").json()
                self.assertEqual(status["state"], "ready")
                self.assertTrue(status["url"].endswith("?fast=true"))
                response = client.get(status["url"], headers={"Range": "bytes=0-31"})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, fast.read_bytes()[:32])
                self.assertEqual(client.head(status["url"]).status_code, 200)
                fast.write_bytes(b"incomplete")
                self.assertEqual(client.get(status["url"]).status_code, 202)

    def complete_fast(self, spec):
        self.service.fast_preview_path(spec).write_bytes(b"0000ftyp" + b"fast-preview" * 8)

    def complete_storyboard(self, spec):
        self.complete_fast(spec)
        directory = self.service.storyboards.directory(spec.key)
        directory.mkdir(parents=True, exist_ok=True)
        count = math.ceil(spec.source["duration_ms"] / 1000)
        names = [f"sheet-{index:05d}.jpg" for index in range(math.ceil(count / 100))]
        for name in names:
            (directory / name).write_bytes(b"\xff\xd8storyboard-sheet\xff\xd9")
        manifest = {"version": 1, "interval_ms": 1000, "tile_width": 160, "tile_height": 90,
                    "columns": 10, "rows": 10, "frame_count": count, "sheets": names}
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def complete_batch_item(self, spec):
        spec.target.write_bytes(b"finished-preview")
        spec.thumbnail_target.write_bytes(b"\xff\xd8thumbnail\xff\xd9")
        self.complete_storyboard(spec)

    def test_batch_ready_requires_preview_thumbnail_and_storyboard_and_get_is_readonly(self):
        for index, video in enumerate(self.project["videos"]):
            spec = self.service.preview_spec(self.project, video["id"])
            spec.target.write_bytes(b"finished-preview")
            if index in {0, 2, 3}:
                spec.thumbnail_target.write_bytes(b"\xff\xd8thumbnail\xff\xd9")
            if index != 2:
                self.complete_storyboard(spec)
            if index == 1:
                spec.thumbnail_target.write_bytes(b"truncated JPEG")
            elif index == 3:
                (self.service.storyboards.directory(spec.key) / "sheet-00000.jpg").write_bytes(b"truncated JPEG")
        with patch.object(self.service.previews, "render") as render:
            with TestClient(self.app, base_url="http://127.0.0.1") as client:
                before = self.service.load(self.project["id"])
                response = client.get(f"/api/projects/{self.project['id']}/prepare")
                self.assertEqual(response.status_code, 200)
                status = response.json()
                self.assertEqual((status["state"], status["total"], status["ready"]), ("idle", 4, 1))
                self.assertEqual(status["running"], 0)
                self.assertEqual(status["queued"], 0)
                self.assertTrue(all(item["state"] == "idle" for item in status["items"][1:]))
                render.assert_not_called()
                self.assertEqual(self.service.load(self.project["id"]), before)
                self.assertFalse(list(self.service.preparations.glob("*.json")))

    def test_interactive_preheat_is_not_a_requested_full_batch(self):
        release = threading.Event()

        def render(spec, update, stopping):
            while not release.wait(0.01):
                if stopping.is_set():
                    raise HTTPException(503, "停止")
            self.complete_batch_item(spec)

        self.service.previews.render = render
        ident = self.project["id"]
        self.service.prepare_preview(ident, "v0001", prefetch=False)
        status = self.service.preparation_status(ident)
        self.assertEqual(status["state"], "running")
        self.assertFalse(status["requested"])
        self.service.prepare_project(ident)
        self.assertTrue(self.service.preparation_status(ident)["requested"])
        release.set()
        wait_until(lambda: self.service.preparation_status(ident)["state"] == "ready")

    def test_batch_api_nonblocking_completion_and_repeat_reuses_everything(self):
        release, started = threading.Event(), threading.Event()
        calls = []

        def render(spec, update, stopping):
            calls.append(spec.key)
            update(25)
            started.set()
            while not release.wait(0.01):
                if stopping.is_set():
                    raise HTTPException(503, "停止")
            self.complete_batch_item(spec)

        self.service.previews.render = render
        ident = self.project["id"]
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            before = self.service.load(ident)
            start = time.monotonic()
            response = client.post(f"/api/projects/{ident}/prepare", json={"retry_failed": False})
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(response.json()["state"], "running")
            self.assertTrue(started.wait(2))
            status = client.get(f"/api/projects/{ident}/prepare").json()
            self.assertEqual((status["running"], status["queued"], status["ready"]), (1, 3, 0))
            self.assertGreater(status["progress"], 0)
            release.set()
            wait_until(lambda: self.service.preparation_status(ident)["state"] == "ready")
            status = client.post(f"/api/projects/{ident}/prepare").json()
            self.assertEqual((status["ready"], status["progress"]), (4, 100))
            self.assertEqual(len(calls), 4)
        self.assertEqual(self.service.load(ident), before)
        restarted = ProjectService(self.root)
        try:
            self.assertEqual(restarted.preparation_status(ident)["state"], "ready")
            self.assertIsNone(restarted.previews.worker)
        finally:
            restarted.previews.close()

    def test_batch_partial_failure_persists_and_explicit_retry_only_runs_failed_item(self):
        calls, fail = [], [True]
        bad = self.service.preview_spec(self.project, "v0002")

        def render(spec, update, stopping):
            calls.append(spec.key)
            if spec.key == bad.key and fail[0]:
                raise HTTPException(422, "第二段处理失败。")
            self.complete_batch_item(spec)

        self.service.previews.render = render
        ident = self.project["id"]
        self.service.prepare_project(ident)
        wait_until(lambda: self.service.preparation_status(ident)["state"] == "partial")
        status = self.service.preparation_status(ident)
        self.assertEqual((status["ready"], status["failed"]), (3, 1))
        self.assertEqual(status["items"][1]["detail"], "第二段处理失败。")
        self.service.prepare_project(ident)
        self.assertEqual(len(calls), 4)
        self.service.previews.close()
        restarted = ProjectService(self.root)
        restarted.previews.render = render
        try:
            self.assertEqual(restarted.preparation_status(ident)["state"], "partial")
            restarted.prepare_project(ident)
            self.assertEqual(len(calls), 4)
            fail[0] = False
            restarted.prepare_project(ident, retry_failed=True)
            wait_until(lambda: restarted.preparation_status(ident)["state"] == "ready")
            self.assertEqual(calls.count(bad.key), 2)
            self.assertEqual(len(calls), 5)
        finally:
            restarted.previews.close()

    def test_batch_restart_pauses_and_explicit_resume_keeps_completed_results(self):
        second_started = threading.Event()
        calls = []
        first = self.service.preview_spec(self.project, "v0001")

        def interrupted_render(spec, update, stopping):
            calls.append(spec.key)
            if spec.key == first.key:
                self.complete_batch_item(spec)
                return
            second_started.set()
            stopping.wait(5)
            raise HTTPException(503, "平台已关闭")

        self.service.previews.render = interrupted_render
        ident = self.project["id"]
        self.service.prepare_project(ident)
        self.assertTrue(second_started.wait(2))
        self.service.previews.close()
        restarted = ProjectService(self.root)
        try:
            status = restarted.preparation_status(ident)
            self.assertEqual((status["state"], status["ready"], status["failed"], status["queued"]), ("paused", 1, 0, 0))
            self.assertIsNone(restarted.previews.worker)
            resumed = []

            def render(spec, update, stopping):
                resumed.append(spec.key)
                self.complete_batch_item(spec)

            restarted.previews.render = render
            restarted.prepare_project(ident)
            wait_until(lambda: restarted.preparation_status(ident)["state"] == "ready")
            self.assertEqual(len(resumed), 3)
            self.assertNotIn(first.key, resumed)
        finally:
            restarted.previews.close()

    def test_batch_cached_video_only_prepares_missing_thumbnails_without_reencoding(self):
        thumbnail_calls = []
        for video in self.project["videos"]:
            spec = self.service.preview_spec(self.project, video["id"])
            spec.target.write_bytes(b"finished-preview")
            self.complete_storyboard(spec)

        def thumbnail(spec):
            thumbnail_calls.append(spec.key)
            spec.thumbnail_target.write_bytes(b"\xff\xd8thumbnail\xff\xd9")
            return spec.thumbnail_target

        with patch("backend.service.run_ffmpeg") as encode, patch.object(self.service, "render_thumbnail", side_effect=thumbnail):
            self.service.prepare_project(self.project["id"])
            wait_until(lambda: self.service.preparation_status(self.project["id"])["state"] == "ready")
            self.assertEqual(len(thumbnail_calls), 4)
            encode.assert_not_called()

    def test_batch_reports_missing_source_and_continues_other_videos(self):
        self.paths[1].unlink()
        self.service.previews.render = lambda spec, update, stopping: self.complete_batch_item(spec)
        self.service.prepare_project(self.project["id"])
        wait_until(lambda: self.service.preparation_status(self.project["id"])["state"] == "partial")
        status = self.service.preparation_status(self.project["id"])
        self.assertEqual((status["ready"], status["failed"]), (3, 1))
        self.assertEqual(status["items"][1]["video_id"], "v0002")
        self.assertIn("不可访问", status["items"][1]["detail"])

    def test_source_change_rejected_even_with_cached_preview(self):
        spec = self.service.preview_spec(self.project, "v0001")
        spec.target.write_bytes(b"old-preview")
        self.paths[0].write_bytes(b"changed-source-content")
        with self.assertRaises(HTTPException) as error:
            self.service.preview_status(self.project["id"], "v0001")
        self.assertEqual(error.exception.status_code, 409)

    def test_offline_complete_current_and_legacy_caches_survive_restart_and_serve_range(self):
        ident = self.project["id"]
        for index, video in enumerate(self.project["videos"]):
            spec = self.service.preview_spec(self.project, video["id"])
            preview = spec.target if index % 2 == 0 else spec.legacy_preview
            thumbnail = spec.thumbnail_target if index % 2 == 0 else spec.legacy_thumbnail
            preview.write_bytes(b"offline-preview-complete")
            thumbnail.write_bytes(b"\xff\xd8offline-thumbnail\xff\xd9")
            self.complete_storyboard(spec)
        for path in self.paths:
            path.unlink()
        restarted = create_app(self.root, auth_required=False)
        with patch.object(restarted.state.service.previews, "render") as render:
            with TestClient(restarted, base_url="http://127.0.0.1") as client:
                before = restarted.state.service.load(ident)
                for video in self.project["videos"]:
                    url = f"/api/previews/{ident}/{video['id']}"
                    self.assertEqual(client.get(url).json()["state"], "ready")
                    self.assertEqual(client.post(url, json={"prefetch": True}).json()["state"], "ready")
                    response = client.get(video["url"], headers={"Range": "bytes=0-6"})
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.content, b"offline")
                    self.assertEqual(client.get(video["thumbnail_url"]).content, b"\xff\xd8offline-thumbnail\xff\xd9")
                for method in (client.get, client.post):
                    response = method(f"/api/projects/{ident}/prepare")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual((response.json()["state"], response.json()["ready"], response.json()["failed"]), ("ready", 4, 0))
                render.assert_not_called()
                self.assertIsNone(restarted.state.service.previews.worker)
                self.assertEqual(restarted.state.service.load(ident), before)

    def test_offline_missing_preview_is_an_error_even_with_thumbnail_and_partial_file(self):
        ident = self.project["id"]
        for video in self.project["videos"]:
            self.complete_batch_item(self.service.preview_spec(self.project, video["id"]))
        missing = self.service.preview_spec(self.project, "v0001")
        missing.target.write_bytes(b"")
        missing.target.with_name(missing.target.stem + ".partial.mp4").write_bytes(b"incomplete-preview")
        self.paths[0].unlink()
        with patch.object(self.service.previews, "render") as render:
            with TestClient(self.app, base_url="http://127.0.0.1") as client:
                for method, url in ((client.get, f"/api/previews/{ident}/v0001"),
                                    (client.post, f"/api/previews/{ident}/v0001"),
                                    (client.get, f"/api/media/{ident}/v0001")):
                    response = method(url)
                    self.assertEqual(response.status_code, 404)
                    self.assertIn("不可访问", response.json()["detail"])
                status = client.post(f"/api/projects/{ident}/prepare").json()
                self.assertEqual((status["state"], status["ready"], status["failed"]), ("partial", 3, 1))
                self.assertEqual(status["items"][0]["state"], "error")
                render.assert_not_called()

    def test_native_compatible_video_without_local_copy_is_not_ready_offline(self):
        path = self.root / "A09999_20260914100000_0100.mp4"
        path.write_bytes(b"synthetic-native-source")
        self.service.probe = lambda path: {"duration_ms": 1000, "media_start_seconds": 0, "format_start_seconds": 0, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": ["aac"]}
        project = self.service.create([path], "Native preview", None)
        self.service.save(project)
        self.assertEqual(self.service.preview_status(project["id"], "v0001")["state"], "ready")
        path.unlink()
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            response = client.get(project["videos"][0]["url"])
            self.assertEqual(response.status_code, 404)
            self.assertIn("不可访问", response.json()["detail"])
            status = client.post(f"/api/projects/{project['id']}/prepare").json()
            self.assertEqual((status["ready"], status["failed"]), (0, 1))
            self.assertEqual(status["items"][0]["state"], "error")

    def test_replaced_source_mtime_rejects_cached_media_thumbnail_and_preparation(self):
        ident = self.project["id"]
        for video in self.project["videos"]:
            self.complete_batch_item(self.service.preview_spec(self.project, video["id"]))
        path = self.paths[0]
        stamp = path.stat()
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
        with patch.object(self.service.previews, "render") as render:
            with TestClient(self.app, base_url="http://127.0.0.1") as client:
                for method, url in ((client.get, f"/api/previews/{ident}/v0001"),
                                    (client.post, f"/api/previews/{ident}/v0001"),
                                    (client.get, f"/api/media/{ident}/v0001"),
                                    (client.get, f"/api/thumbnails/{ident}/v0001"),
                                    (client.get, f"/api/storyboards/{ident}/v0001"),
                                    (client.get, f"/api/storyboards/{ident}/v0001/sheets/0")):
                    response = method(url)
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("已更改", response.json()["detail"])
                for method in (client.get, client.post):
                    status = method(f"/api/projects/{ident}/prepare").json()
                    self.assertEqual((status["ready"], status["failed"]), (3, 1))
                    self.assertIn("已更改", status["items"][0]["detail"])
                render.assert_not_called()

    def test_offline_cached_video_generates_missing_thumbnails_from_cache_without_reencoding(self):
        expected_inputs = set()
        for video in self.project["videos"]:
            spec = self.service.preview_spec(self.project, video["id"])
            spec.target.write_bytes(b"finished-preview")
            expected_inputs.add(str(spec.target))
            self.complete_storyboard(spec)
        for path in self.paths:
            path.unlink()
        before = self.service.load(self.project["id"])
        inputs = []
        self.service.tool = lambda name: Path("ffmpeg.exe")

        def thumbnail_command(args, **kwargs):
            inputs.append(args[args.index("-i") + 1])
            Path(args[-1]).write_bytes(b"\xff\xd8offline-generated-thumbnail\xff\xd9")

        with patch("backend.service.run_ffmpeg") as encode, patch.object(self.service, "command", side_effect=thumbnail_command):
            self.service.prepare_project(self.project["id"])
            wait_until(lambda: self.service.preparation_status(self.project["id"])["state"] == "ready")
            self.assertCountEqual(inputs, expected_inputs)
            encode.assert_not_called()
            self.assertEqual(self.service.load(self.project["id"]), before)

    def test_storyboard_get_and_sheet_get_are_readonly_and_ready_metadata_survives_offline_restart(self):
        ident = self.project["id"]
        url = f"/api/storyboards/{ident}/v0001"
        with patch.object(self.service.previews, "render") as render:
            with TestClient(self.app, base_url="http://127.0.0.1") as client:
                before = self.service.load(ident)
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["state"], "idle")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(client.get(url + "/sheets/0").status_code, 404)
                render.assert_not_called()
                self.assertIsNone(self.service.previews.worker)
                self.assertEqual(self.service.load(ident), before)
        spec = self.service.preview_spec(self.service.load(ident), "v0001")
        self.complete_batch_item(spec)
        self.paths[0].unlink()
        restarted = create_app(self.root, auth_required=False)
        with patch.object(restarted.state.service.previews, "render") as render:
            with TestClient(restarted, base_url="http://127.0.0.1") as client:
                before = restarted.state.service.load(ident)
                response = client.get(url)
                status = response.json()
                self.assertEqual(status["state"], "ready")
                self.assertEqual((status["interval_ms"], status["frame_count"]), (1000, 1))
                self.assertEqual((status["tile_width"], status["tile_height"], status["columns"], status["rows"]), (160, 90, 10, 10))
                self.assertEqual(len(status["sheets"]), 1)
                self.assertTrue(status["sheets"][0].startswith(url + "/sheets/0?"))
                sheet = client.get(status["sheets"][0])
                self.assertEqual(sheet.status_code, 200)
                self.assertEqual(sheet.headers["Content-Type"], "image/jpeg")
                self.assertEqual(sheet.content, b"\xff\xd8storyboard-sheet\xff\xd9")
                self.assertEqual(client.head(status["sheets"][0]).status_code, 200)
                self.assertEqual(client.get(url + "/sheets/-1").status_code, 404)
                self.assertEqual(client.get(url + "/sheets/1").status_code, 404)
                render.assert_not_called()
                self.assertIsNone(restarted.state.service.previews.worker)
                self.assertEqual(restarted.state.service.load(ident), before)

    def test_batch_upgrades_cached_video_and_thumbnail_with_storyboards_offline_without_reencoding(self):
        ident = self.project["id"]
        expected = {}
        for video in self.project["videos"]:
            spec = self.service.preview_spec(self.project, video["id"])
            spec.target.write_bytes(b"finished-preview")
            spec.thumbnail_target.write_bytes(b"\xff\xd8thumbnail\xff\xd9")
            self.complete_fast(spec)
            expected[spec.key] = spec
        for path in self.paths:
            path.unlink()
        calls = []
        self.service.tool = lambda name: Path("ffmpeg.exe")

        def storyboard(source, key, duration_ms, ffmpeg, update, stopping):
            self.assertEqual(source, expected[key].target)
            self.assertEqual(duration_ms, 1000)
            self.assertFalse(expected[key].path.exists())
            calls.append(key)
            update(50)
            return self.complete_storyboard(expected[key])

        with patch("backend.service.run_ffmpeg") as encode, patch.object(self.service.storyboards, "render", side_effect=storyboard):
            with TestClient(self.app, base_url="http://127.0.0.1") as client:
                before = self.service.load(ident)
                status = client.get(f"/api/projects/{ident}/prepare").json()
                self.assertEqual((status["state"], status["ready"]), ("idle", 0))
                self.assertEqual(client.get(f"/api/previews/{ident}/v0001").json()["state"], "ready")
                self.assertFalse(calls)
                client.post(f"/api/projects/{ident}/prepare")
                wait_until(lambda: self.service.preparation_status(ident)["state"] == "ready")
                self.assertCountEqual(calls, expected)
                status = client.post(f"/api/projects/{ident}/prepare").json()
                self.assertEqual((status["ready"], status["failed"]), (4, 0))
                self.assertEqual(len(calls), 4)
                encode.assert_not_called()
                self.assertEqual(self.service.load(ident), before)

    def test_encoder_args_preserve_frames_limit_threads_and_disable_stdin(self):
        spec = self.service.preview_spec(self.project, "v0001")
        self.service.tool = lambda name: Path("ffmpeg.exe")
        observed = []

        def run(args, duration_ms, log_path, update, stopping):
            observed.extend(args)
            Path(args[-1]).write_bytes(b"preview")

        with patch("backend.service.run_ffmpeg", side_effect=run):
            self.service.render_video(spec, lambda value: None, threading.Event())
        self.assertIn("-nostdin", observed)
        self.assertEqual(observed[observed.index("-preset") + 1], "superfast")
        self.assertEqual(observed[observed.index("-fps_mode") + 1], "passthrough")
        self.assertNotIn("-r", observed)
        self.assertNotIn("-t", observed)
        self.assertEqual(observed[observed.index("-vf") + 1], "setpts=PTS-STARTPTS,scale=trunc(iw/2)*2:trunc(ih/2)*2")
        self.assertEqual([observed[index + 1] for index, arg in enumerate(observed) if arg == "-threads"], ["2", "4"])


if __name__ == "__main__":
    unittest.main()
