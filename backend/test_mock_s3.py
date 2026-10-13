"""The mock path must complete a real project without touching NAS state."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import create_app


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools required")
class MockS3WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="datamark-mock-test-")
        self.root = Path(self.temporary.name)
        self.video = self.root / "usb" / "video" / "A00001_20261010120000_0001.avi"
        self.video.parent.mkdir(parents=True)
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5",
                        "-t", "1", "-c:v", "mpeg4", "-q:v", "12", "-y", str(self.video)], check=True)
        self.catalog_dir = self.root / "desktop" / "1001test"
        self.catalog_dir.mkdir(parents=True)
        shutil.copy2(self.video, self.catalog_dir / self.video.name)
        self.environment = patch.dict(os.environ, {"DATAMARK_S3_MOCK": "1", "FFMPEG_PATH": shutil.which("ffmpeg"),
                                                  "FFPROBE_PATH": shutil.which("ffprobe"),
                                                  "DATAMARK_S3_MOCK_SOURCE_DIR": str(self.catalog_dir),
                                                  "DATAMARK_S3_MOCK_SOURCE_KEY": "daily/1001test/20261010"})
        self.environment.start()
        self.app = create_app(self.root)
        self.app.state.auth.create_user("worker", "标注员", "another long safe password")
        self.app.state.auth.create_user("other", "其他标注员", "a different long password")
        self.client = TestClient(self.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
        self.client.__enter__()
        login = self.client.post("/api/auth/login", json={"username": "worker", "password": "another long safe password"})
        self.assertEqual(login.status_code, 200, login.text)
        self.csrf = {"X-CSRF-Token": login.json()["csrf"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def test_local_mock_uses_native_desktop_media(self):
        cache = self.app.state.service.desktop_cache
        cache.enabled = True
        cache.override = str(self.root / "local-desktop-cache")
        opened = self.client.post("/api/storage/projects/open", headers=self.csrf,
                                  json={"path": "daily/1001test/20261010", "name": "本机测试"})
        self.assertEqual(opened.status_code, 200, opened.text)
        ident = opened.json()["id"]
        self.client.post(f"/api/projects/{ident}/session/prepare", json={}, headers=self.csrf)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/projects/{ident}/session").json()
            if status["state"] in {"ready", "partial", "error"}:
                break
            time.sleep(.2)
        self.assertEqual(status["state"], "ready", status)
        manifest = self.client.get(f"/api/projects/{ident}/session/manifest").json()
        self.assertEqual(manifest["cache_location"]["mode"], "native")
        url = manifest["videos"][0]["url"]
        self.assertIn("/api/session-media/", url)
        self.assertEqual(self.client.get(url, headers={"Range": "bytes=0-31"}).status_code, 206)
        self.assertTrue(list((self.root / "local-desktop-cache").rglob("*.mp4")))

    def test_catalog_signed_playback_draft_and_submission(self):
        self.assertIn("s3-mock", self.client.get("/api/health").json()["capabilities"])
        self.assertEqual(self.client.post("/api/projects/open", json={"path": "/mnt/nas/anything"}, headers=self.csrf).status_code, 404)
        self.assertEqual(self.client.get("/api/sources/browse").status_code, 404)
        root_listing = self.client.get("/api/storage/sources").json()
        self.assertEqual([item["path"] for item in root_listing["entries"]], ["daily"])
        self.assertEqual(self.client.get("/api/mock-s3/sources", params={"prefix": "daily"}).json()["entries"][0]["path"], "daily/1001test")
        date_listing = self.client.get("/api/mock-s3/sources", params={"prefix": "daily/1001test"}).json()
        self.assertEqual(date_listing["entries"][0]["path"], "daily/1001test/20261010")
        self.assertFalse(date_listing["can_open"])
        folder_listing = self.client.get("/api/storage/sources", params={"path": "daily/1001test/20261010"}).json()
        self.assertTrue(folder_listing["can_open"])
        self.assertEqual(folder_listing["entries"][0]["path"], f"daily/1001test/20261010/{self.video.name}")
        self.assertEqual(self.client.get("/api/mock-s3/sources", params={"prefix": "../outside"}).status_code, 422)
        self.assertEqual(self.client.get("/api/storage/sources", params={"path": "../outside"}).status_code, 422)
        self.assertEqual(self.client.get("/api/storage/sources", params={"page": 1}).status_code, 422)
        self.assertEqual(self.client.post("/api/mock-s3/projects/open", headers=self.csrf,
                                          json={"path": "daily"}).status_code, 404)
        uploaded = self.client.post("/api/storage/projects/open", headers=self.csrf,
                                    json={"path": "daily/1001test/20261010", "name": "测试项目"})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        project = uploaded.json()
        ident = project["id"]
        self.assertEqual(project["storage_mode"], "s3-mock")
        self.assertEqual(project["storage_prefix"], "daily/1001test/20261010")
        self.assertIsNone(project["source_dir"])
        self.assertNotIn("/mnt/nas", json.dumps(project))
        self.assertEqual(self.client.get(f"/api/projects/{ident}").json()["storage_mode"], "s3-mock")
        self.assertNotIn(str(self.root), json.dumps(project))
        self.assertFalse((self.catalog_dir / ".datamark-cache").exists())
        self.assertEqual(self.client.get(f"/api/media/{ident}/{project['videos'][0]['id']}").status_code, 404)

        started = self.client.post(f"/api/projects/{ident}/session/prepare", json={}, headers=self.csrf)
        self.assertEqual(started.status_code, 200, started.text)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/projects/{ident}/session").json()
            if status["state"] in {"ready", "partial", "error"}:
                break
            time.sleep(.2)
        self.assertEqual(status["state"], "ready", status)
        manifest_response = self.client.get(f"/api/projects/{ident}/session/manifest")
        self.assertEqual(manifest_response.status_code, 200, manifest_response.text)
        manifest = manifest_response.json()
        url = manifest["videos"][0]["url"]
        self.assertIn("/mock-objects/media/", url)
        self.assertEqual(self.client.get(f"/api/session-media/{ident}/{manifest['version']}/{project['videos'][0]['id']}").status_code, 404)
        with TestClient(self.app, base_url="http://127.0.0.1") as anonymous:
            self.assertEqual(anonymous.get(url).status_code, 200)
            self.assertEqual(anonymous.get(url, headers={"Range": "bytes=0-31"}).status_code, 206)
            self.assertEqual(anonymous.get(url.replace("signature=", "signature=0")).status_code, 403)

        project = self.client.get(f"/api/projects/{ident}").json()
        annotations = project["annotations"]
        annotations["posture"] = [{"id": "p1", "label": "动", "kind": "interval", "start_ms": 0,
                                   "end_ms": project["duration_ms"]}]
        saved = self.client.put(f"/api/projects/{ident}/draft", json={"annotations": annotations, "expected_revision": project["revision"]},
                                headers=self.csrf)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.client.put(f"/api/projects/{ident}/draft", json={"annotations": annotations,
                               "expected_revision": 0}, headers=self.csrf).status_code, 409)
        submitted = self.client.post(f"/api/projects/{ident}/writeback", json={"expected_revision": saved.json()["revision"]}, headers=self.csrf)
        self.assertEqual(submitted.status_code, 200, submitted.text)
        store = self.app.state.mock_s3
        pointer = json.loads((store.objects / "daily/1001test/20261010/timeline/.latest.json").read_text())
        self.assertEqual(len(pointer["keys"]), len(saved.json()["selected_tracks"]))
        self.assertEqual(pointer["save_id"], submitted.json()["save_id"])
        for key in pointer["keys"]:
            self.assertEqual(json.loads((store.objects / key).read_text())["schema_version"], 3)

        with TestClient(self.app, base_url="http://127.0.0.1") as other:
            login = other.post("/api/auth/login", json={"username": "other", "password": "a different long password"})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(other.get(f"/api/projects/{ident}/session/manifest").status_code, 403)
            self.assertEqual(other.post("/api/mock-s3/projects/open", headers={"X-CSRF-Token": login.json()["csrf"]},
                                        json={"path": "daily/1001test/20261010"}).status_code, 403)

    def test_named_custom_track_is_written_and_preserved_when_unselected(self):
        opened = self.client.post("/api/storage/projects/open", headers=self.csrf,
                                  json={"path": "daily/1001test/20261010"})
        self.assertEqual(opened.status_code, 200, opened.text)
        project = opened.json()
        self.assertEqual(project["fixed_tracks"], ["scene", "posture", "category"])
        ident = project["id"]
        library = self.client.post("/api/account/track-library", headers=self.csrf,
                                   json={"id": "food", "name": "饮食", "mode": "state", "labels": ["进食"]})
        self.assertEqual(library.status_code, 200, library.text)
        created = self.client.put(f"/api/projects/{ident}/track-selection", headers=self.csrf,
                                  json={"tracks": [*project["selected_tracks"], "food"], "expected_revision": project["revision"]})
        self.assertEqual(created.status_code, 200, created.text)
        annotations = created.json()["annotations"]
        annotations["posture"] = [{"id": "pose", "label": "坐", "start_ms": 0, "end_ms": project["duration_ms"]}]
        annotations["food"] = [{"id": "meal", "label": "进食", "start_ms": 0, "end_ms": project["duration_ms"]}]
        saved = self.client.put(f"/api/projects/{ident}/draft", headers=self.csrf,
                                json={"annotations": annotations, "expected_revision": created.json()["revision"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        submitted = self.client.post(f"/api/projects/{ident}/writeback", headers=self.csrf,
                                     json={"expected_revision": saved.json()["revision"]})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        path = self.app.state.mock_s3.objects / "daily/1001test/20261010/timeline/food.timeline.json"
        self.assertEqual(json.loads(path.read_text())["segments"][0]["label"], "进食")
        preserved = path.read_bytes()
        removed = self.client.put(f"/api/projects/{ident}/track-selection", headers=self.csrf,
                                  json={"tracks": project["selected_tracks"], "expected_revision": saved.json()["revision"]})
        self.assertEqual(removed.status_code, 200, removed.text)
        resubmitted = self.client.post(f"/api/projects/{ident}/writeback", headers=self.csrf,
                                       json={"expected_revision": removed.json()["revision"]})
        self.assertEqual(resubmitted.status_code, 200, resubmitted.text)
        self.assertEqual(path.read_bytes(), preserved)

    def test_mock_writeback_only_selected_axis_preserves_previous_file(self):
        opened = self.client.post("/api/storage/projects/open", headers=self.csrf,
                                  json={"path": "daily/1001test/20261010"})
        self.assertEqual(opened.status_code, 200, opened.text)
        ident = opened.json()["id"]
        service = self.app.state.service
        raw = service.load(ident)
        raw["selected_tracks"] = ["posture"]
        service.save(raw)
        first = self.client.post(f"/api/projects/{ident}/writeback", headers=self.csrf,
                                 json={"expected_revision": raw["revision"]})
        self.assertEqual(first.status_code, 200, first.text)
        folder = self.app.state.mock_s3.objects / "daily/1001test/20261010/timeline"
        self.assertEqual({path.name for path in folder.glob("*.timeline.json")}, {"posture.timeline.json"})
        preserved = (folder / "posture.timeline.json").read_bytes()
        raw = service.load(ident)
        raw["selected_tracks"] = ["scene"]
        service.save(raw)
        second = self.client.post(f"/api/projects/{ident}/writeback", headers=self.csrf,
                                  json={"expected_revision": raw["revision"]})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual((folder / "posture.timeline.json").read_bytes(), preserved)
        pointer = json.loads((folder / ".latest.json").read_bytes())
        self.assertEqual(pointer["keys"], second.json()["paths"])
        self.assertEqual(len(pointer["keys"]), 1)

    def test_folder_only_selection_does_not_accept_video_uploads(self):
        uploaded = self.client.post("/api/mock-s3/projects", headers=self.csrf,
                                    files=[("files", (self.video.name, self.video.read_bytes(), "video/mp4"))])
        self.assertEqual(uploaded.status_code, 404, uploaded.text)
        self.assertEqual(self.client.get("/api/projects").json(), [])

    def test_authorized_upload_publishes_compressed_fpv_and_timeline(self):
        if getattr(self, 'native_upload', False):
            self.app.state.service.desktop_cache.enabled = True
            self.app.state.service.desktop_cache.override = str(self.root / 'Desktop' / 'dataMarkProject')
        self.assertEqual(self.client.post("/api/mock-s3/uploads/pick", headers=self.csrf).status_code, 403)
        worker = next(user for user in self.app.state.auth.list_users() if user["username"] == "worker")
        self.app.state.auth.set_upload_permission(worker["id"], True)
        with patch("backend.app.choose_local_paths", return_value=[str(self.video)]):
            picked = self.client.post("/api/mock-s3/uploads/pick", headers=self.csrf)
        self.assertEqual(picked.status_code, 200, picked.text)
        self.assertEqual(picked.json()["files"], [{"name": self.video.name, "size": self.video.stat().st_size}])
        self.assertNotIn(str(self.video.parent), picked.text)
        body = {"collector_name": "张三", "uploader_name": "", "note": "例行采集",
                "selection_id": picked.json()["selection_id"]}
        started = self.client.post("/api/mock-s3/uploads", json=body, headers=self.csrf)
        self.assertEqual(started.status_code, 200, started.text)
        task = started.json()
        self.assertEqual(task["prefix"], "daily/20261010/1010张三")
        self.assertEqual(task["uploader_name"], "标注员")
        self.assertEqual(task["uploaded_by_username"], "worker")
        staging = self.app.state.mock_uploads.staging
        self.assertFalse(staging.is_relative_to(self.app.state.mock_s3.root))
        self.assertEqual(list((staging / task["id"]).rglob("*.avi")), [])
        self.assertEqual(self.client.put(f"/api/mock-s3/uploads/{task['id']}/files/0", content=self.video.read_bytes(),
                                         headers=self.csrf).status_code, 410)
        self.assertFalse((self.app.state.mock_s3.objects / task["prefix"]).exists())
        finished = self.client.post(f"/api/mock-s3/uploads/{task['id']}/finish", headers=self.csrf)
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["state"], "ready")
        capture = self.app.state.mock_s3.objects / task["prefix"]
        compressed = capture / "FPV" / self.video.with_suffix(".mp4").name
        self.assertTrue(compressed.is_file())
        self.assertFalse((capture / self.video.name).exists())
        self.assertFalse((capture / "video").exists())
        self.assertFalse((capture / "IMU").exists())
        self.assertFalse((capture / "HEART").exists())
        self.assertFalse((staging / task["id"]).exists())
        published = json.loads((capture / "manifest.json").read_text())
        self.assertEqual((published["collector_name"], published["uploader_name"], published["note"]),
                         ("张三", "标注员", "例行采集"))
        self.assertEqual(published["uploaded_by_username"], "worker")
        self.assertEqual(self.app.state.service.probe(compressed)["codec"], "h264")
        if getattr(self, 'native_upload', False):
            from backend.desktop_cache import sha256_file
            directory = self.root / 'Desktop' / 'dataMarkProject' / '1010张三'
            cached = list(directory.glob('*.normal.mp4'))
            self.assertEqual(len(cached), 1)
            self.assertEqual(sha256_file(cached[0]), sha256_file(compressed))
            self.upload_cache = cached[0]
        listing = self.client.get("/api/mock-s3/sources", params={"prefix": task["prefix"]}).json()
        self.assertTrue(listing["can_open"])
        self.assertEqual(listing["capture"]["collector_name"], "张三")
        self.assertEqual(listing["capture"]["uploaded_by_username"], "worker")
        opened = self.client.post("/api/mock-s3/projects/open", headers=self.csrf,
                                  json={"path": task["prefix"]})
        self.assertEqual(opened.status_code, 200, opened.text)
        project = opened.json()
        ident = project["id"]
        self.assertEqual(project["videos"][0]["relative_path"], f"FPV/{compressed.name}")
        started = self.client.post(f"/api/projects/{ident}/session/prepare", json={}, headers=self.csrf)
        self.assertEqual(started.status_code, 200, started.text)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self.client.get(f"/api/projects/{ident}/session").json()["state"]
            if state in {"ready", "partial", "error"}:
                break
            time.sleep(.2)
        self.assertEqual(state, "ready")
        manifest = self.client.get(f"/api/projects/{ident}/session/manifest").json()
        if getattr(self, 'native_upload', False):
            self.assertIn('/api/session-media/', manifest['videos'][0]['url'])
            self.assertEqual(manifest['collection_name'], '1010张三')
            self.assertEqual(len(list(directory.glob('*.normal.mp4'))), 1)
            self.assertTrue(self.upload_cache.exists())
        else:
            self.assertIn(f"/mock-objects/{quote(task['prefix'])}/FPV/", manifest["videos"][0]["url"])
        self.assertEqual(self.client.get(manifest["videos"][0]["url"]).status_code, 200)
        project = self.client.get(f"/api/projects/{ident}").json()
        annotations = project["annotations"]
        annotations["posture"] = [{"id": "p1", "label": "动", "kind": "interval", "start_ms": 0,
                                   "end_ms": project["duration_ms"]}]
        saved = self.client.put(f"/api/projects/{ident}/draft", json={"annotations": annotations, "expected_revision": project["revision"]},
                                headers=self.csrf)
        self.assertEqual(saved.status_code, 200, saved.text)
        submitted = self.client.post(f"/api/projects/{ident}/writeback", json={"expected_revision": saved.json()["revision"]}, headers=self.csrf)
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(len(list((capture / "timeline").glob("*.timeline.json"))), len(saved.json()["selected_tracks"]))
        self.assertEqual(json.loads((capture / "timeline/posture.timeline.json").read_text())["schema_version"], 3)
        self.assertEqual(self.client.post("/api/mock-s3/uploads", json=body, headers=self.csrf).status_code, 404)
        with patch("backend.app.choose_local_paths", return_value=[str(self.video)]):
            again = self.client.post("/api/mock-s3/uploads/pick", headers=self.csrf)
        self.assertEqual(self.client.post("/api/mock-s3/uploads",
            json={**body, "selection_id": again.json()["selection_id"]}, headers=self.csrf).status_code, 409)
        self.app.state.auth.set_upload_permission(worker["id"], False)
        self.assertEqual(self.client.post("/api/mock-s3/uploads", json={**body,"collector_name":"another"}, headers=self.csrf).status_code, 403)

    def test_native_upload_retains_compressed_cache_for_annotation(self):
        self.native_upload = True
        self.test_authorized_upload_publishes_compressed_fpv_and_timeline()

    def test_upload_rejects_non_video_files(self):
        worker = next(user for user in self.app.state.auth.list_users() if user["username"] == "worker")
        self.app.state.auth.set_upload_permission(worker["id"], True)
        with patch("backend.app.choose_local_paths", return_value=[]):
            self.assertEqual(self.client.post("/api/mock-s3/uploads/pick", headers=self.csrf).status_code, 204)
        txt = self.video.parent / "device.txt"
        txt.write_text("test", encoding="utf-8")
        with patch("backend.app.choose_local_paths", return_value=[str(txt)]):
            self.assertEqual(self.client.post("/api/mock-s3/uploads/pick", headers=self.csrf).status_code, 422)
        with patch("backend.app.choose_local_paths", return_value=[str(self.video), str(txt)]):
            self.assertEqual(self.client.post("/api/mock-s3/uploads/pick", headers=self.csrf).status_code, 422)

    def test_native_picker_upload_is_loopback_only(self):
        worker = next(user for user in self.app.state.auth.list_users() if user["username"] == "worker")
        self.app.state.auth.set_upload_permission(worker["id"], True)
        with TestClient(self.app, base_url="http://127.0.0.1", client=("203.0.113.5", 50000)) as remote:
            login = remote.post("/api/auth/login", json={"username": "worker", "password": "another long safe password"})
            self.assertEqual(login.status_code, 200)
            with patch("backend.app.choose_local_paths", side_effect=AssertionError("picker must stay closed")):
                denied = remote.post("/api/mock-s3/uploads/pick", headers={"X-CSRF-Token": login.json()["csrf"]})
            self.assertEqual(denied.status_code, 403)

    def test_changed_usb_video_is_not_published(self):
        worker = next(user for user in self.app.state.auth.list_users() if user["username"] == "worker")
        self.app.state.auth.set_upload_permission(worker["id"], True)
        with patch("backend.app.choose_local_paths", return_value=[str(self.video)]):
            picked = self.client.post("/api/mock-s3/uploads/pick", headers=self.csrf)
        self.assertEqual(picked.status_code, 200, picked.text)
        started = self.client.post("/api/mock-s3/uploads", headers=self.csrf,
            json={"collector_name": "张三", "selection_id": picked.json()["selection_id"]})
        self.assertEqual(started.status_code, 200, started.text)
        previous = self.video.stat()
        os.utime(self.video, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
        rejected = self.client.post(f"/api/mock-s3/uploads/{started.json()['id']}/finish", headers=self.csrf)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertFalse((self.app.state.mock_s3.objects / started.json()["prefix"]).exists())

    def test_mock_store_rejects_uncompressed_source(self):
        prepared = self.root / "prepared"
        video = prepared / "FPV" / "source.mp4"
        video.parent.mkdir(parents=True)
        shutil.copy2(self.video, video)
        manifest = {"prefix": "daily/20261010/1010张三", "files": [{"key": "FPV/source.mp4",
            "size": video.stat().st_size, "sha256": self.app.state.mock_s3._file_digest(video)}]}
        with self.assertRaises(Exception) as error:
            self.app.state.mock_s3.publish_capture(manifest["prefix"], prepared, manifest, self.app.state.service)
        self.assertEqual(getattr(error.exception, "status_code", None), 422)
        self.assertFalse((self.app.state.mock_s3.objects / manifest["prefix"]).exists())

    def test_revoked_uploader_cannot_publish_and_can_discard_staging(self):
        worker = next(user for user in self.app.state.auth.list_users() if user["username"] == "worker")
        self.app.state.auth.set_upload_permission(worker["id"], True)
        with patch("backend.app.choose_local_paths", return_value=[str(self.video)]):
            picked = self.client.post("/api/mock-s3/uploads/pick", headers=self.csrf)
        self.assertEqual(picked.status_code, 200, picked.text)
        started = self.client.post("/api/mock-s3/uploads", headers=self.csrf,
                                   json={"collector_name": "another", "uploader_name": "代传人员",
                                         "selection_id": picked.json()["selection_id"]})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["uploader_name"], "代传人员")
        self.assertIsNone(started.json()["note"])
        ident = started.json()["id"]
        self.app.state.auth.set_upload_permission(worker["id"], False)
        self.assertEqual(self.client.post(f"/api/mock-s3/uploads/{ident}/finish", headers=self.csrf).status_code, 403)
        self.assertFalse((self.app.state.mock_s3.objects / started.json()["prefix"]).exists())
        self.assertEqual(self.client.request("DELETE", f"/api/mock-s3/uploads/{ident}", headers=self.csrf).status_code, 200)
        self.assertFalse((self.app.state.mock_uploads.staging / ident).exists())

    def test_deployment_switch_keeps_existing_projects_on_their_own_storage(self):
        nas_root = self.root / "mounted-nas"
        capture = nas_root / "habit" / "smoking" / "1010worker"
        fpv = capture / "FPV"
        fpv.mkdir(parents=True)
        shutil.copy2(self.video, fpv / self.video.name)
        mock = self.client.post("/api/mock-s3/projects/open", headers=self.csrf,
                                json={"path": "daily/1001test/20261010"})
        self.assertEqual(mock.status_code, 200, mock.text)
        self.assertEqual(mock.json()["storage_mode"], "s3-mock")
        with patch.dict(os.environ, {"DATAMARK_MOCK_NAS_ROOT": str(nas_root),
                                          "DATAMARK_STORAGE_BACKEND": "nas"}):
            app = create_app(self.root)
            with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001)) as client:
                login = client.post("/api/auth/login", json={"username": "worker", "password": "another long safe password"})
                self.assertEqual(login.status_code, 200, login.text)
                csrf = {"X-CSRF-Token": login.json()["csrf"]}
                health = client.get("/api/health").json()
                self.assertEqual(health["storage_backend"], "nas")
                capabilities = health["capabilities"]
                self.assertIn("nas-source-browser", capabilities)
                self.assertIn("s3-mock", capabilities)
                self.assertEqual(client.get("/api/sources/browse").status_code, 200)
                self.assertEqual(client.get("/api/storage/sources").status_code, 200)
                self.assertEqual(client.get("/api/mock-s3/sources").status_code, 409)
                self.assertEqual(client.post("/api/mock-s3/projects/open", headers=csrf,
                    json={"path": "daily/1001test/20261010"}).status_code, 409)
                self.assertEqual(client.post("/api/projects/open", headers=csrf,
                    json={"path": str(self.catalog_dir)}).status_code, 403)
                nas = client.post("/api/storage/projects/open", headers=csrf, json={"path": str(capture)})
                self.assertEqual(nas.status_code, 200, nas.text)
                project = nas.json()
                self.assertEqual(project["storage_mode"], "nas")
                self.assertEqual(len(client.get("/api/projects").json()), 2)
                for item, expected_path in ((mock.json(), "/mock-objects/"), (project, "/api/session-media/")):
                    started = client.post(f"/api/projects/{item['id']}/session/prepare", headers=csrf, json={})
                    self.assertEqual(started.status_code, 200, started.text)
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        status = client.get(f"/api/projects/{item['id']}/session").json()
                        if status["state"] in {"ready", "partial", "error"}:
                            break
                        time.sleep(.2)
                    self.assertEqual(status["state"], "ready", status)
                    manifest = client.get(f"/api/projects/{item['id']}/session/manifest")
                    self.assertEqual(manifest.status_code, 200, manifest.text)
                    self.assertIn(expected_path, manifest.json()["videos"][0]["url"])
                self.assertEqual(client.post(f"/api/projects/{mock.json()['id']}/videos/open", headers=csrf,
                    json={"path": str(capture), "expected_revision": 0}).status_code, 409)
                project = client.get(f"/api/projects/{project['id']}").json()
                annotations = project["annotations"]
                annotations["posture"] = [{"id": "p1", "label": "动", "kind": "interval", "start_ms": 0,
                                           "end_ms": project["duration_ms"]}]
                saved = client.put(f"/api/projects/{project['id']}/draft", headers=csrf,
                    json={"annotations": annotations, "expected_revision": project["revision"]})
                self.assertEqual(saved.status_code, 200, saved.text)
                submitted = client.post(f"/api/projects/{project['id']}/writeback", headers=csrf,
                    json={"expected_revision": saved.json()["revision"]})
                self.assertEqual(submitted.status_code, 200, submitted.text)
                self.assertTrue((capture / "timeline" / "posture.timeline.json").is_file())
                self.assertFalse((app.state.mock_s3.objects / "daily/1001test/20261010/timeline").exists())
                disconnected = self.root / "nas-disconnected"
                nas_root.rename(disconnected)
                try:
                    self.assertEqual(client.get(f"/api/projects/{mock.json()['id']}").status_code, 200)
                finally:
                    disconnected.rename(nas_root)
                with TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.5", 50002)) as remote:
                    self.assertEqual(remote.post("/api/auth/login", json={"username": "worker",
                        "password": "another long safe password"}).status_code, 403)
                    self.assertEqual(remote.get("/api/sources/browse").status_code, 403)
        with patch.dict(os.environ, {"DATAMARK_MOCK_NAS_ROOT": str(nas_root),
                                          "DATAMARK_STORAGE_BACKEND": "s3-mock"}):
            app = create_app(self.root)
            with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50003)) as client:
                login = client.post("/api/auth/login", json={"username": "worker",
                    "password": "another long safe password"})
                self.assertEqual(login.status_code, 200)
                csrf = {"X-CSRF-Token": login.json()["csrf"]}
                self.assertEqual(client.get("/api/health").json()["storage_backend"], "s3-mock")
                self.assertEqual(client.get("/api/mock-s3/sources").status_code, 200)
                self.assertEqual(client.post("/api/projects/open", headers=csrf,
                    json={"path": str(capture)}).status_code, 409)
                self.assertEqual(client.get(f"/api/projects/{project['id']}").json()["storage_mode"], "nas")

    def test_unavailable_storage_backend_fails_startup(self):
        with patch.dict(os.environ, {"DATAMARK_STORAGE_BACKEND": "s3"}):
            with self.assertRaisesRegex(ValueError, "not available"):
                create_app(self.root)
