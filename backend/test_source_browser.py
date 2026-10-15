from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.auth import AuthStore
from backend.service import AXES, FILENAMES, ProjectService
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
        try:
            (self.nas / "escape").symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows does not grant symlink privileges")
            raise
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
                self.assertEqual(project["selected_tracks"], ["scene", "posture", "category"])
                self.assertEqual({path.name for path in (source / "timeline").glob("*.timeline.json")},
                                 {FILENAMES[axis] for axis in project["selected_tracks"]})
                other_login = client.post("/api/auth/login", json={"username": "other", "password": "correct horse battery staple"})
                other_csrf = {"X-CSRF-Token": other_login.json()["csrf"]}
                self.assertEqual(client.get(f'/api/projects/{project["id"]}').status_code, 403)
                self.assertEqual(client.post("/api/projects/open", json={"path": str(source)}, headers=other_csrf).status_code, 409)
                selected = client.post("/api/projects/files", json={"paths": [str(video)]}, headers=other_csrf)
                self.assertEqual(selected.status_code, 422, selected.text)

    def test_unassigned_existing_directory_can_be_claimed_by_first_annotator(self):
        source = self.nas / "采集"
        source.mkdir()
        (source / "A09999_20260917120000_0000.avi").write_bytes(b"fixture video")
        auth = AuthStore(self.root)
        auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        first = auth.create_user("first", "第一位", "correct horse battery staple", "annotator")
        auth.create_user("second", "第二位", "correct horse battery staple", "annotator")
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}), patch.object(ProjectService, "probe", return_value={"duration_ms": 1000, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []}):
            app = create_app(self.root, source_root=self.nas)
            with TestClient(app, base_url="https://10.20.30.40") as client:
                admin = client.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery staple"})
                opened = client.post("/api/projects/open", json={"path": str(source)}, headers={"X-CSRF-Token": admin.json()["csrf"]})
                self.assertEqual(opened.status_code, 200, opened.text)
                project_id = opened.json()["id"]
                self.assertIsNone(auth.assignment(project_id))
                worker = client.post("/api/auth/login", json={"username": "first", "password": "correct horse battery staple"})
                claimed = client.post("/api/projects/open", json={"path": str(source)}, headers={"X-CSRF-Token": worker.json()["csrf"]})
                self.assertEqual(claimed.status_code, 200, claimed.text)
                self.assertEqual(claimed.json()["id"], project_id)
                self.assertEqual(auth.assignment(project_id), first["id"])
                other = client.post("/api/auth/login", json={"username": "second", "password": "correct horse battery staple"})
                self.assertEqual(client.post("/api/projects/open", json={"path": str(source)}, headers={"X-CSRF-Token": other.json()["csrf"]}).status_code, 409)

    def test_takeover_requires_two_confirmations_and_revokes_old_owner(self):
        source = self.nas / "采集"
        source.mkdir()
        (source / "A09999_20260917120000_0000.avi").write_bytes(b"fixture video")
        auth = AuthStore(self.root)
        auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        first = auth.create_user("first", "第一位", "correct horse battery staple", "annotator")
        second = auth.create_user("second", "第二位", "correct horse battery staple", "annotator")
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}), patch.object(ProjectService, "probe", return_value={"duration_ms": 1000, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []}):
            app = create_app(self.root, source_root=self.nas)
            with TestClient(app, base_url="https://10.20.30.40") as client:
                first_login = client.post("/api/auth/login", json={"username": "first", "password": "correct horse battery staple"})
                first_csrf = {"X-CSRF-Token": first_login.json()["csrf"]}
                project = client.post("/api/projects/open", json={"path": str(source)}, headers=first_csrf).json()
                saved = client.put(f'/api/projects/{project["id"]}/draft', json={"annotations": complete_annotations(project), "expected_revision": project["revision"]}, headers=first_csrf)
                self.assertEqual(saved.status_code, 200, saved.text)
                second_login = client.post("/api/auth/login", json={"username": "second", "password": "correct horse battery staple"})
                second_csrf = {"X-CSRF-Token": second_login.json()["csrf"]}
                inspection = client.post("/api/source-claims/inspect", json={"path": str(source)}, headers=second_csrf)
                self.assertEqual(inspection.status_code, 200, inspection.text)
                claim = inspection.json()
                self.assertTrue(claim["requires_takeover"])
                self.assertFalse(claim["has_json"])
                self.assertEqual(claim["owner_name"], "第一位")
                body = {"path": str(source), "claim_mode": "keep", "claim_token": claim["claim_token"]}
                self.assertEqual(client.post("/api/projects/open", json={**body, "takeover_confirmations": 1}, headers=second_csrf).status_code, 409)
                taken = client.post("/api/projects/open", json={**body, "takeover_confirmations": 2}, headers=second_csrf)
                self.assertEqual(taken.status_code, 200, taken.text)
                self.assertEqual(taken.json()["id"], project["id"])
                self.assertEqual(taken.json()["annotations"], saved.json()["annotations"])
                self.assertEqual(auth.assignment(project["id"]), second["id"])
                self.assertNotEqual(first["id"], second["id"])
                client.post("/api/auth/login", json={"username": "first", "password": "correct horse battery staple"})
                self.assertEqual(client.get(f'/api/projects/{project["id"]}').status_code, 403)

    def test_existing_nas_json_choice_can_backup_and_start_blank(self):
        source = self.nas / "采集"
        source.mkdir()
        (source / "A09999_20260917120000_0000.avi").write_bytes(b"fixture video")
        auth = AuthStore(self.root)
        auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        worker = auth.create_user("worker", "标注者", "correct horse battery staple", "annotator")
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}), patch.object(ProjectService, "probe", return_value={"duration_ms": 1000, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []}):
            app = create_app(self.root, source_root=self.nas)
            with TestClient(app, base_url="https://10.20.30.40") as client:
                admin = client.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery staple"})
                admin_csrf = {"X-CSRF-Token": admin.json()["csrf"]}
                project = client.post("/api/projects/open", json={"path": str(source)}, headers=admin_csrf).json()
                annotations = complete_annotations(project)
                changed = client.put(f'/api/projects/{project["id"]}/draft', json={"annotations": annotations, "expected_revision": project["revision"]}, headers=admin_csrf)
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertEqual(client.post(f'/api/projects/{project["id"]}/writeback', headers=admin_csrf).status_code, 200)
                old = {axis: (source / 'timeline' / FILENAMES[axis]).read_bytes() for axis in project["selected_tracks"]}
                self.assertEqual({path.name for path in (source / "timeline").glob("*.timeline.json")}, {FILENAMES[axis] for axis in old})
                worker_login = client.post("/api/auth/login", json={"username": "worker", "password": "correct horse battery staple"})
                worker_csrf = {"X-CSRF-Token": worker_login.json()["csrf"]}
                claim = client.post("/api/source-claims/inspect", json={"path": str(source)}, headers=worker_csrf).json()
                self.assertTrue(claim["has_json"])
                self.assertFalse(claim["requires_takeover"])
                body = {"path": str(source), "claim_mode": "restart", "claim_token": claim["claim_token"]}
                self.assertEqual(client.post("/api/projects/open", json={"path": str(source)}, headers=worker_csrf).status_code, 409)
                (source / 'timeline' / FILENAMES['scene']).write_bytes(old['scene'] + b'\n')
                self.assertEqual(client.post("/api/projects/open", json=body, headers=worker_csrf).status_code, 409)
                (source / 'timeline' / FILENAMES['scene']).write_bytes(old['scene'])
                claim = client.post("/api/source-claims/inspect", json={"path": str(source)}, headers=worker_csrf).json()
                body['claim_token'] = claim['claim_token']
                opened = client.post("/api/projects/open", json=body, headers=worker_csrf)
                self.assertEqual(opened.status_code, 200, opened.text)
                fresh = opened.json()
                self.assertEqual(fresh['id'], project['id'])
                self.assertTrue(all(not rows for rows in fresh['annotations'].values()))
                self.assertEqual(auth.assignment(project['id']), worker['id'])
                archive = source / '.annotation-backups' / fresh['source_backup_id']
                self.assertEqual({axis: (archive / FILENAMES[axis]).read_bytes() for axis in old}, old)
                self.assertEqual({axis: (source / 'timeline' / FILENAMES[axis]).read_bytes() for axis in old}, old)
                self.assertEqual(client.post(f'/api/projects/{project["id"]}/writeback', headers=worker_csrf).status_code, 200)
                self.assertEqual(json.loads((source / 'timeline' / FILENAMES['scene']).read_bytes())['segments'], [])

    def test_first_platform_claim_backs_up_existing_nas_json_before_blank_start(self):
        source = self.nas / "采集"
        source.mkdir()
        (source / "A09999_20260917120000_0000.avi").write_bytes(b"fixture video")
        with patch.object(ProjectService, "probe", return_value={"duration_ms": 1000, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []}):
            writer = ProjectService(self.root / "writer")
            original = writer.open_path(str(source))
            writer.update_draft(original["id"], complete_annotations(original), original["revision"])
            writer.writeback(original["id"])
            old = {axis: (source / "timeline" / FILENAMES[axis]).read_bytes() for axis in original["fixed_tracks"] if axis != "habit"}
            self.assertEqual({path.name for path in (source / "timeline").glob("*.timeline.json")}, {FILENAMES[axis] for axis in old})
            app_root = self.root / "new-platform"
            auth = AuthStore(app_root)
            auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
            auth.create_user("worker", "标注者", "correct horse battery staple", "annotator")
            with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}):
                app = create_app(app_root, source_root=self.nas)
                with TestClient(app, base_url="https://10.20.30.40") as client:
                    login = client.post("/api/auth/login", json={"username": "worker", "password": "correct horse battery staple"})
                    csrf = {"X-CSRF-Token": login.json()["csrf"]}
                    claim = client.post("/api/source-claims/inspect", json={"path": str(source)}, headers=csrf).json()
                    self.assertTrue(claim["has_json"])
                    self.assertIsNone(claim["project_name"])
                    opened = client.post("/api/projects/open", json={"path": str(source), "claim_mode": "restart",
                        "claim_token": claim["claim_token"]}, headers=csrf)
                    self.assertEqual(opened.status_code, 200, opened.text)
                    fresh = opened.json()
                    self.assertTrue(all(not rows for rows in fresh["annotations"].values()))
                    backup = source / ".annotation-backups" / fresh["source_backup_id"]
                    self.assertEqual({axis: (backup / FILENAMES[axis]).read_bytes() for axis in old}, old)
                    self.assertEqual({axis: (source / "timeline" / FILENAMES[axis]).read_bytes() for axis in old}, old)

    def test_takeover_can_continue_nas_json_while_archiving_dirty_platform_draft(self):
        source = self.nas / "采集"
        source.mkdir()
        (source / "A09999_20260917120000_0000.avi").write_bytes(b"fixture video")
        auth = AuthStore(self.root)
        auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        auth.create_user("first", "第一位", "correct horse battery staple", "annotator")
        second = auth.create_user("second", "第二位", "correct horse battery staple", "annotator")
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}), patch.object(ProjectService, "probe", return_value={"duration_ms": 1000, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []}):
            app = create_app(self.root, source_root=self.nas)
            with TestClient(app, base_url="https://10.20.30.40") as client:
                first = client.post("/api/auth/login", json={"username": "first", "password": "correct horse battery staple"})
                first_csrf = {"X-CSRF-Token": first.json()["csrf"]}
                project = client.post("/api/projects/open", json={"path": str(source)}, headers=first_csrf).json()
                original = complete_annotations(project)
                saved = client.put(f'/api/projects/{project["id"]}/draft', json={"annotations": original, "expected_revision": project["revision"]}, headers=first_csrf).json()
                self.assertEqual(client.post(f'/api/projects/{project["id"]}/writeback', headers=first_csrf).status_code, 200)
                stored_json = (source / 'timeline' / FILENAMES['scene']).read_bytes()
                dirty = {**saved['annotations'], 'scene': [{**saved['annotations']['scene'][0], 'label': '室外'}]}
                current = app.state.service.load(project['id'])
                dirty_response = client.put(f'/api/projects/{project["id"]}/draft', json={"annotations": dirty, "expected_revision": current['revision']}, headers=first_csrf)
                self.assertEqual(dirty_response.status_code, 200, dirty_response.text)
                second_login = client.post("/api/auth/login", json={"username": "second", "password": "correct horse battery staple"})
                second_csrf = {"X-CSRF-Token": second_login.json()["csrf"]}
                claim = client.post("/api/source-claims/inspect", json={"path": str(source)}, headers=second_csrf).json()
                self.assertTrue(claim['requires_takeover'] and claim['has_json'] and claim['has_platform_draft'])
                response = client.post("/api/projects/open", json={"path": str(source), "claim_mode": "continue",
                    "claim_token": claim['claim_token'], "takeover_confirmations": 2}, headers=second_csrf)
                self.assertEqual(response.status_code, 200, response.text)
                continued = response.json()
                self.assertEqual(continued['annotations']['scene'], saved['annotations']['scene'])
                self.assertEqual((source / 'timeline' / FILENAMES['scene']).read_bytes(), stored_json)
                recovery = app.state.service.storage.directory(project['id']) / 'recoveries' / f"{continued['recovery_id']}.json"
                self.assertEqual(json.loads(recovery.read_text())['annotations']['scene'], dirty_response.json()['annotations']['scene'])
                self.assertEqual(auth.assignment(project['id']), second['id'])

    def test_claimed_video_cannot_be_imported_again_through_directory(self):
        source = self.nas / "采集"
        source.mkdir()
        video = source / "A09999_20260917120000_0000.avi"
        video.write_bytes(b"fixture video")
        auth = AuthStore(self.root)
        auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        first = auth.create_user("first", "第一位", "correct horse battery staple", "annotator")
        auth.create_user("second", "第二位", "correct horse battery staple", "annotator")
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}), patch.object(ProjectService, "probe", return_value={"duration_ms": 1000, "codec": "h264", "pixel_format": "yuv420p", "audio_codecs": []}):
            legacy = ProjectService(self.root).open_files([str(video)], source_writeback=False)
            auth.claim(legacy["id"], first["id"])
            app = create_app(self.root, source_root=self.nas)
            with TestClient(app, base_url="https://10.20.30.40") as client:
                second = client.post("/api/auth/login", json={"username": "second", "password": "correct horse battery staple"})
                opened = client.post("/api/projects/open", json={"path": str(source)}, headers={"X-CSRF-Token": second.json()["csrf"]})
                self.assertEqual(opened.status_code, 409, opened.text)
