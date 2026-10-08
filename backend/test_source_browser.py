from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.auth import AuthStore
from backend.service import ProjectService
from backend.source_browser import PAGE_SIZE, browse_sources
from backend.testing_annotations import complete_annotations


class SourceBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-browser-")
        self.root = Path(self.temp.name)
        self.nas = self.root / "nas"
        self.nas.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_browse_lists_only_directories_and_videos_in_nas(self):
        folder = self.nas / "采集"
        folder.mkdir()
        (self.nas / "recording.avi").write_bytes(b"video")
        (self.nas / "notes.txt").write_text("private")
        (self.nas / ".datamark-cache").mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.nas / "escape").symlink_to(outside)
        listing = browse_sources(self.nas)
        self.assertEqual([(item["name"], item["kind"]) for item in listing["entries"]],
                         [("采集", "directory"), ("recording.avi", "file")])
        self.assertEqual(listing["entries"][1]["size"], 5)
        self.assertIsNone(listing["parent"])
        self.assertEqual(browse_sources(self.nas, str(folder))["parent"], str(self.nas.resolve()))
        for forbidden in (str(outside), str(self.nas / "escape"), str(self.nas / ".." / "outside")):
            with self.subTest(forbidden=forbidden), self.assertRaises(HTTPException):
                browse_sources(self.nas, forbidden)

    def test_large_directory_has_stable_pages(self):
        for index in range(PAGE_SIZE + 1):
            (self.nas / f"clip-{index:03}.mp4").touch()
        first = browse_sources(self.nas)
        second = browse_sources(self.nas, page=1)
        self.assertEqual(len(first["entries"]), PAGE_SIZE)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(second["entries"]), 1)
        self.assertFalse(second["has_more"])
        self.assertNotEqual(first["entries"][-1]["path"], second["entries"][0]["path"])

    def test_intranet_browser_requires_login(self):
        auth = AuthStore(self.root)
        auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        auth.create_user("worker", "标注者", "correct horse battery staple", "annotator")
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}):
            app = create_app(self.root, source_root=self.nas)
        with TestClient(app, base_url="https://10.20.30.40") as client:
            self.assertEqual(client.get("/api/sources/browse").status_code, 401)
            worker = client.post("/api/auth/login", json={"username": "worker", "password": "correct horse battery staple"})
            self.assertEqual(worker.status_code, 200, worker.text)
            self.assertEqual(client.get("/api/sources/browse").status_code, 200)
            admin = client.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery staple"})
            self.assertEqual(admin.status_code, 200, admin.text)
            listing = client.get("/api/sources/browse")
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertEqual(listing.json()["root"], str(self.nas.resolve()))
            self.assertIn("nas-source-browser", client.get("/api/health").json()["capabilities"])

    def test_annotator_imports_assigned_nas_project_and_writes_back(self):
        source = self.nas / "采集"
        source.mkdir()
        video = source / "A09999_20260917120000_0000.avi"
        video.write_bytes(b"fixture video")
        outsider = self.root / "outside.avi"
        outsider.write_bytes(b"not on NAS")
        auth = AuthStore(self.root)
        auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        worker = auth.create_user("worker", "标注者", "correct horse battery staple", "annotator")
        auth.create_user("other", "其他标注者", "correct horse battery staple", "annotator")
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}), patch.object(ProjectService, "probe", return_value={"duration_ms": 1000, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []}):
            app = create_app(self.root, source_root=self.nas)
            with TestClient(app, base_url="https://10.20.30.40") as client:
                login = client.post("/api/auth/login", json={"username": "worker", "password": "correct horse battery staple"})
                csrf = {"X-CSRF-Token": login.json()["csrf"]}
                denied = client.post("/api/projects/open", json={"path": str(outsider)}, headers=csrf)
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(client.get("/api/sources/browse", params={"path": str(self.root)}).status_code, 403)
                opened = client.post("/api/projects/open", json={"path": str(source)}, headers=csrf)
                self.assertEqual(opened.status_code, 200, opened.text)
                project = opened.json()
                self.assertEqual(auth.assignment(project["id"]), worker["id"])
                self.assertEqual(len(client.get("/api/projects").json()), 1)
                saved = client.put(f'/api/projects/{project["id"]}/draft', json={"annotations": complete_annotations(project), "expected_revision": project["revision"]}, headers=csrf)
                self.assertEqual(saved.status_code, 200, saved.text)
                writeback = client.post(f'/api/projects/{project["id"]}/writeback', headers=csrf)
                self.assertEqual(writeback.status_code, 200, writeback.text)
                self.assertEqual(len(list((source / "timeline").glob("*.timeline.json"))), 4)
                other_login = client.post("/api/auth/login", json={"username": "other", "password": "correct horse battery staple"})
                other_csrf = {"X-CSRF-Token": other_login.json()["csrf"]}
                self.assertEqual(client.get(f'/api/projects/{project["id"]}').status_code, 403)
                self.assertEqual(client.post("/api/projects/open", json={"path": str(source)}, headers=other_csrf).status_code, 403)
                selected = client.post("/api/projects/files", json={"paths": [str(video)]}, headers=other_csrf)
                self.assertEqual(selected.status_code, 200, selected.text)
                self.assertIsNone(selected.json()["source_dir"])
