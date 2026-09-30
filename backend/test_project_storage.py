from __future__ import annotations
import copy
import json
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.project_storage import import_name, checked_tree, link_copy

ROOT = Path(__file__).resolve().parents[1]

class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="storage-test-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.app = create_app(self.root, auth_required=False)
        self.s = self.app.state.service
        self.s.probe = lambda path: {"duration_ms": 1000, "codec": "mjpeg", "media_start_seconds": 0, "audio_codecs": []}
        self.original = self.root / "originals"
        self.original.mkdir()
        self.source = self.original / "A09999_20260914090000_0000.avi"
        self.source.write_bytes(b"original-video")

    def tearDown(self):
        self.s.sessions.close()
        self.s.previews.close()
        self.temp.cleanup()

    def project(self, path=None, ident=None):
        p = self.s.create([path or self.source], "Old name", None, ident=ident,
                          imported_at="2026-09-14T12:32:00+00:00")
        p["annotations"]["scene"] = [{"id": "scene", "label": "室内", "start_ms": 0, "end_ms": 1000}]
        p["revision"] = 9
        self.s.save(p)
        return p

    def legacy(self, p):
        for key in ["imported_at", "import_time_estimated", "_storage_version"]:
            p.pop(key, None)
        p["name"] = "VIDEO"
        self.s.save(p)
        spec = self.s.preview_spec(p, "v0001", legacy=True)
        spec.target.write_bytes(b"cached-preview")
        spec.thumbnail_target.write_bytes(b"\xff\xd8cover\xff\xd9")
        self.s.fast_preview_path(spec).write_bytes(b"0000ftyp" + b"fast" * 10)
        directory = self.s.storyboards.directory(spec.key)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sheet-00000.jpg").write_bytes(b"\xff\xd8sheet\xff\xd9")
        (directory / "manifest.json").write_text(json.dumps({"version": 1, "interval_ms": 1000,
            "tile_width": 160, "tile_height": 90, "columns": 10, "rows": 10,
            "frame_count": 1, "sheets": ["sheet-00000.jpg"]}))
        return spec

    def test_names_use_import_time_and_minute_collisions_have_separate_storage(self):
        self.assertEqual(import_name("2026-09-14T12:32:00+00:00"), "0914-20-32")
        a, b = self.project(), self.project()
        self.assertEqual(a["name"], b["name"])
        self.assertNotEqual(self.s.storage.directory(a["id"]), self.s.storage.directory(b["id"]))
        self.s.migrate_legacy_projects()
        self.assertEqual(self.s.load(a["id"])["imported_at"], a["imported_at"])

    def test_existing_uploaded_project_migration_preserves_annotations_and_offline_cache(self):
        ident = uuid.uuid4().hex
        folder = self.s.imports / ident
        folder.mkdir()
        source = folder / self.source.name
        source.write_bytes(b"uploaded-original")
        p = self.project(source, ident)
        old = self.legacy(p)
        before = copy.deepcopy(p)
        (self.s.preparations / (ident + ".json")).write_text(json.dumps({"source_fingerprint": p["source_fingerprint"]}))
        self.s.migrate_legacy_projects()
        migrated = self.s.load(ident)
        for key in ("annotations", "revision", "updated_at", "videos", "source_fingerprint"):
            self.assertEqual(migrated[key], before[key])
        self.assertTrue(migrated["import_time_estimated"])
        self.assertEqual(migrated["name"], import_name(migrated["imported_at"]))
        moved_source = Path(migrated["_sources"]["v0001"]["path"])
        self.assertTrue(moved_source.is_relative_to(self.s.storage.directory(ident)))
        self.assertEqual(moved_source.read_bytes(), b"uploaded-original")
        self.assertFalse(folder.exists())
        self.assertFalse(old.target.exists())
        self.assertTrue((self.s.storage.directory(ident) / "preparation.json").exists())
        moved_source.unlink()  # Playback must remain available when originals are unavailable.
        self.assertEqual(self.s.media(ident, "v0001").read_bytes(), b"cached-preview")
        self.assertTrue(self.s.media(ident, "v0001", fast=True).exists())
        self.assertTrue(self.s.storyboard_sheet(ident, "v0001", 0).exists())
        self.s.migrate_legacy_projects()
        self.assertEqual(self.s.load(ident), migrated)

    def test_local_preview_cleanup_preserves_shared_legacy_cache(self):
        a, b = self.project(), self.project()
        self.legacy(a)
        self.legacy(b)
        self.s.migrate_legacy_projects()
        pa = self.s.media(a["id"], "v0001")
        pb = self.s.media(b["id"], "v0001")
        self.assertNotEqual(pa, pb)
        self.s.clear_local_previews(a["id"], 9)
        self.assertTrue(pa.exists())
        self.assertEqual(pb.read_bytes(), b"cached-preview")
        self.assertTrue(self.s.storyboard_sheet(b["id"], "v0001", 0).exists())
        self.assertEqual(self.source.read_bytes(), b"original-video")

    def test_api_confirmation_revision_and_playback_only_cleanup(self):
        p = self.project()
        external = self.original / "scene.json"
        external.write_text("separately-saved-result")
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            p = self.s.load(p["id"])
            directory = self.s.storage.directory(p["id"])
            for relative in ("preview/full.mp4", "preview/fast.mp4", "imports/copy.avi", "results/scene.json", "process.json",
                             "playback/compact.mp4", "playback/fast20.mp4", "playback/cover.jpg",
                             "playback/sheet-00000.jpg", "playback/manifest.json", "playback/video.assets.json"):
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"project-local-data")
            url = "/api/projects/" + p["id"]
            for body, code in [({"confirmed": False, "expected_revision": 9}, 422),
                               ({"expected_revision": 9}, 422),
                               ({"confirmed": True, "expected_revision": 8}, 409)]:
                self.assertEqual(client.post(url + "/preview-cache/clear", json=body).status_code, code)
                self.assertTrue(directory.exists())
            body = {"confirmed": True, "expected_revision": 9}
            self.assertEqual(client.request("DELETE", url, json=body, headers={"Origin": "https://foreign.example"}).status_code, 403)
            self.assertEqual(client.request("DELETE", url, json=body).status_code, 409)
            self.assertEqual(client.post(url + "/preview-cache/clear", json=body, headers={"Origin": "https://foreign.example"}).status_code, 403)
            self.assertEqual(client.post(url + "/preview-cache/clear", json=body).status_code, 200)
            self.assertFalse((directory / "playback").exists())
            self.assertTrue((directory / "imports/copy.avi").exists())
            self.assertTrue((directory / "results/scene.json").exists())
            self.assertTrue((directory / "preview/full.mp4").exists())
            self.assertEqual(client.get(url).status_code, 200)
            self.assertEqual(self.s.load(p["id"]), p)
            self.assertEqual(client.put(url + "/draft", json={"annotations": p["annotations"], "expected_revision": 9}).status_code, 200)
            self.assertEqual(external.read_text(), "separately-saved-result")
            self.assertEqual(self.source.read_bytes(), b"original-video")

    def test_failed_cleanup_preserves_draft_and_rejects_late_preparation(self):
        p = self.project()
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            p = self.s.load(p["id"])
            local = self.s.storage.directory(p["id"]) / "playback"
            local.mkdir(parents=True, exist_ok=True)
            (local / "preview.mp4").write_bytes(b"preview")
            with patch("backend.service.shutil.rmtree", side_effect=OSError("file in use")):
                with self.assertRaises(HTTPException) as pending:
                    self.s.clear_local_previews(p["id"], 9)
                self.assertEqual(pending.exception.status_code, 409)
                self.assertIn("重试清理", pending.exception.detail)
            url = "/api/projects/" + p["id"]
            self.assertFalse(client.get(url).json()["deletion_pending"])
            self.assertEqual(self.s.load(p["id"]), p)
            self.assertEqual(client.post(url + "/session/prepare", json={}).status_code, 409)
            self.assertEqual(client.post(url + "/session/prepare", json={"cache_generation": 0}).status_code, 409)
            self.assertEqual(client.post(url + "/preview-cache/clear", json={"confirmed": True, "expected_revision": 9}).status_code, 200)
            self.assertFalse(local.exists())
            generation = client.get(url).json()["playback_generation"]
            with patch.object(self.s.sessions, "start", return_value={"state": "running"}) as start:
                self.assertEqual(client.post(url + "/session/prepare", json={"cache_generation": generation}).status_code, 200)
                start.assert_called_once()

    def test_disabled_delete_does_not_cancel_or_remove_preview_work(self):
        a, b = self.project(), self.project()
        sa = self.s.preview_spec(a, "v0001")
        queued = copy.copy(sa)
        queued.key += "_queued"
        sb = self.s.preview_spec(b, "v0001")
        started, other_done = threading.Event(), threading.Event()
        calls = []
        def render(spec, update, cancel):
            calls.append(spec.key)
            if spec.project_id == a["id"]:
                started.set()
                if not cancel.wait(4):
                    raise AssertionError("cancellation was not delivered")
                raise HTTPException(504, "cancelled")
            spec.target.write_bytes(b"other-preview")
            other_done.set()
        self.s.previews.render = render
        self.s.previews.request_batch(a["id"], [sa, queued])
        self.assertTrue(started.wait(2))
        self.s.previews.request_batch(b["id"], [sb])
        with self.assertRaises(HTTPException) as rejected:
            self.s.delete_project(a["id"], 9)
        self.assertEqual(rejected.exception.status_code, 409)
        self.s.previews.cancel_project(a["id"])
        self.assertTrue(other_done.wait(2))
        self.assertNotIn(queued.key, calls)
        self.assertEqual(self.s.load(a["id"]), a)
        self.assertEqual(sb.target.read_bytes(), b"other-preview")
        with self.assertRaises(HTTPException):
            self.s.previews.request([sa])

    def test_directory_escape_and_symlink_are_rejected(self):
        for ident in ("../originals", "", "A" * 32):
            with self.assertRaises(HTTPException):
                self.s.storage.directory(ident)
        p = self.project()
        directory = self.s.storage.directory(p["id"])
        directory.mkdir(exist_ok=True)
        with patch.object(Path, "is_symlink", lambda item: item == directory):
            with self.assertRaises(HTTPException):
                self.s.clear_local_previews(p["id"], 9)
        self.assertFalse(self.s.load(p["id"]).get("_deleting", False))
        with self.assertRaises(HTTPException):
            checked_tree(self.original, self.s.storage.root)

    def test_offline_cleanup_never_resolves_or_reads_source_paths(self):
        p = self.project()
        directory = self.s.storage.directory(p["id"])
        playback = directory / "playback"
        playback.mkdir(parents=True, exist_ok=True)
        (playback / "preview.mp4").write_bytes(b"preview")
        uploaded = directory / "imports" / "only-copy.avi"
        uploaded.parent.mkdir()
        uploaded.write_bytes(b"original-upload")
        p["_sources"]["v0001"]["path"] = str(uploaded)
        p["_relink_cleanup"] = [{"path": str(uploaded), "video_id": "v0001", "sha256": "unverified"}]
        self.s.save(p)
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            if path == self.source or path == self.original:
                raise AssertionError("Cleanup touched NAS")
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve), \
             patch.object(self.s.source_cache, "validate", side_effect=AssertionError("NAS validate")), \
             patch.object(self.s.source_cache, "remove", side_effect=AssertionError("NAS delete")), \
             patch.object(self.s, "cleanup_relinked_sources", side_effect=AssertionError("Original cleanup")):
            self.s.clear_local_previews(p["id"], 9)
        self.assertFalse(playback.exists())
        self.assertEqual(uploaded.read_bytes(), b"original-upload")
        self.assertEqual(self.s.load(p["id"]), p)

    def test_link_inside_playback_is_rejected_without_removing_other_files(self):
        p = self.project()
        playback = self.s.storage.directory(p["id"]) / "playback"
        playback.mkdir(parents=True, exist_ok=True)
        safe = playback / "preview.mp4"
        safe.write_bytes(b"preview")
        linked = playback / "unexpected-link"
        linked.write_bytes(b"external-placeholder")
        with patch.object(Path, "is_symlink", lambda path: path == linked):
            with self.assertRaises(HTTPException) as error:
                self.s.clear_local_previews(p["id"], 9)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(safe.read_bytes(), b"preview")
        self.assertEqual(self.s.load(p["id"]), p)

    def test_source_inside_playback_blocks_cleanup_without_resolving_nas(self):
        p = self.project()
        playback = self.s.storage.directory(p["id"]) / "playback"
        playback.mkdir(parents=True, exist_ok=True)
        original = playback / "unexpected-original.avi"
        original.write_bytes(b"original")
        other = self.project()
        other["_sources"]["v0001"]["path"] = str(original)
        self.s.save(other)
        with self.assertRaises(HTTPException) as error:
            self.s.clear_local_previews(p["id"], 9)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(original.read_bytes(), b"original")
        self.assertEqual(self.s.load(p["id"]), p)

    def test_cleanup_blocks_parallel_starts_but_keeps_concurrent_draft_save(self):
        p = self.project()
        entered, release = threading.Event(), threading.Event()
        invalidate = self.s.sessions.invalidate
        def wait_for_writers(ident):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Cleanup fixture not released")
            invalidate(ident)
        with patch.object(self.s.sessions, "invalidate", side_effect=wait_for_writers), \
             ThreadPoolExecutor(max_workers=1) as pool:
            cleanup = pool.submit(self.s.clear_local_previews, p["id"], 9)
            try:
                self.assertTrue(entered.wait(2))
                for start in (lambda: self.s.sessions.start(p["id"]),
                              lambda: self.s.start_session(p["id"], cache_generation=0)):
                    with self.assertRaises(HTTPException) as error:
                        start()
                    self.assertEqual(error.exception.status_code, 409)
                saved = self.s.update_draft(p["id"], p["annotations"], 9)
            finally:
                release.set()
            cleanup.result(timeout=3)
        self.assertEqual(self.s.load(p["id"])["revision"], saved["revision"])
        self.assertEqual(self.s.load(p["id"])["annotations"], saved["annotations"])
        with self.assertRaises(HTTPException) as error:
            self.s.start_session(p["id"])
        self.assertEqual(error.exception.status_code, 409)

    def test_migration_never_overwrites_conflicting_file(self):
        source, target = self.root / "from", self.root / "to"
        source.write_bytes(b"AAAA")
        target.write_bytes(b"BBBB")
        with self.assertRaises(HTTPException):
            link_copy(source, target)
        self.assertEqual(target.read_bytes(), b"BBBB")
