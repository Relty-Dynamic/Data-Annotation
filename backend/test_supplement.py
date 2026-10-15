"""Removed supplement endpoints and compatibility with previously composed projects."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.service import empty_annotations

ROOT = Path(__file__).resolve().parents[1]


class LegacyCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legacy-composition-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.app = create_app(self.root, auth_required=False)
        self.service = self.app.state.service
        self.source = self.root / "originals"
        self.source.mkdir()
        self.service.probe = lambda path: {
            "duration_ms": 1000, "codec": "mjpeg", "pixel_format": "yuvj420p",
            "audio_codecs": [], "media_start_seconds": 0,
        }

    def tearDown(self):
        self.service.sessions.close()
        self.service.previews.close()
        self.assertEqual(self.root.resolve().parent, (ROOT / ".tmp").resolve())
        self.temp.cleanup()

    def video(self, index, directory=None):
        path = (directory or self.source) / f"A09999_202609161200{index:02d}_{index:04d}.avi"
        path.write_bytes(b"synthetic-video-" + path.name.encode())
        return path

    def legacy_project(self):
        first = self.video(0)
        external = self.root / "older-supplement"
        external.mkdir()
        second = self.video(2, external)
        project = self.service.create([first, second], "fixture", self.source)
        project["_supplemented"] = True
        project["draft_dirty"] = True
        project["annotations"] = empty_annotations()
        project["annotations"]["habit"] = [
            {"id": "water", "label": "喝水", "kind": "point", "lane": 1,
             "start_ms": 2250, "end_ms": 2250},
        ]
        self.service.save(project)
        return project

    def test_supplement_routes_and_capability_are_removed_without_mutating_project(self):
        project = self.legacy_project()
        incoming = self.video(4)
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            before = self.service.load(project["id"])
            self.assertNotIn("supplement-import", client.get("/api/health").json()["capabilities"])
            for suffix, body in (
                ("open", {"path": str(self.source), "expected_revision": 0}),
                ("files", {"paths": [str(incoming)], "expected_revision": 0}),
                ("upload", {"expected_revision": 0}),
            ):
                with self.subTest(suffix=suffix):
                    response = client.post(f'/api/projects/{project["id"]}/videos/{suffix}', json=body)
                    self.assertEqual(response.status_code, 404, response.text)
                    self.assertEqual(self.service.load(project["id"]), before)
        self.assertFalse(hasattr(self.service, "supplement"))
        self.assertFalse(hasattr(self.service, "supplement_path"))
        self.assertTrue(incoming.is_file())

    def test_reopen_preserves_old_composition_sources_and_child_annotations(self):
        project = self.legacy_project()
        before = self.service.load(project["id"])
        reopened = self.service.open_path(str(self.source))
        self.assertEqual(reopened["id"], before["id"])
        self.assertEqual(reopened["videos"], before["videos"])
        self.assertEqual(reopened["annotations"], before["annotations"])
        self.assertEqual(self.service.load(project["id"])["_sources"], before["_sources"])
        self.assertEqual(len(reopened["videos"]), 2)

    def test_changed_original_still_blocks_reopen(self):
        project = self.legacy_project()
        before = self.service.load(project["id"])
        original = Path(before["_sources"]["v0001"]["path"])
        stamp = original.stat()
        os.utime(original, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10000000))
        with self.assertRaises(HTTPException) as conflict:
            self.service.open_path(str(self.source))
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(self.service.load(project["id"]), before)

    def test_new_files_do_not_change_saved_composition_or_offer_removed_action(self):
        project = self.legacy_project()
        self.video(4)
        before = self.service.load(project["id"])
        with self.assertRaises(HTTPException) as conflict:
            self.service.open_path(str(self.source))
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertIn("从项目列表打开原项目", conflict.exception.detail)
        self.assertEqual(self.service.load(project["id"]), before)


if __name__ == "__main__":
    unittest.main()
