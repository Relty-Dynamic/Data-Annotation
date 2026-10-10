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
        self.video = self.root / "A00001_20261010120000_0001.avi"
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
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        login = self.client.post("/api/auth/login", json={"username": "worker", "password": "another long safe password"})
        self.assertEqual(login.status_code, 200, login.text)
        self.csrf = {"X-CSRF-Token": login.json()["csrf"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def test_catalog_signed_playback_draft_and_submission(self):
        self.assertIn("s3-mock", self.client.get("/api/health").json()["capabilities"])
        self.assertEqual(self.client.post("/api/projects/open", json={"path": "/mnt/nas/anything"}, headers=self.csrf).status_code, 404)
        self.assertEqual(self.client.get("/api/sources/browse").status_code, 404)
        root_listing = self.client.get("/api/mock-s3/sources").json()
        self.assertEqual([item["path"] for item in root_listing["entries"]], ["daily"])
        self.assertEqual(self.client.get("/api/mock-s3/sources", params={"prefix": "daily"}).json()["entries"][0]["path"], "daily/1001test")
        date_listing = self.client.get("/api/mock-s3/sources", params={"prefix": "daily/1001test"}).json()
        self.assertEqual(date_listing["entries"][0]["path"], "daily/1001test/20261010")
        self.assertFalse(date_listing["can_open"])
        folder_listing = self.client.get("/api/mock-s3/sources", params={"prefix": "daily/1001test/20261010"}).json()
        self.assertTrue(folder_listing["can_open"])
        self.assertEqual(folder_listing["entries"][0]["path"], f"daily/1001test/20261010/{self.video.name}")
        self.assertEqual(self.client.get("/api/mock-s3/sources", params={"prefix": "../outside"}).status_code, 422)
        self.assertEqual(self.client.post("/api/mock-s3/projects/open", headers=self.csrf,
                                          json={"path": "daily"}).status_code, 404)
        uploaded = self.client.post("/api/mock-s3/projects/open", headers=self.csrf,
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

        annotations = project["annotations"]
        annotations["posture"] = [{"id": "p1", "label": "动", "kind": "interval", "start_ms": 0,
                                   "end_ms": project["duration_ms"]}]
        saved = self.client.put(f"/api/projects/{ident}/draft", json={"annotations": annotations, "expected_revision": 0},
                                headers=self.csrf)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.client.put(f"/api/projects/{ident}/draft", json={"annotations": annotations,
                               "expected_revision": 0}, headers=self.csrf).status_code, 409)
        submitted = self.client.post(f"/api/projects/{ident}/writeback", json={"expected_revision": 1}, headers=self.csrf)
        self.assertEqual(submitted.status_code, 200, submitted.text)
        store = self.app.state.mock_s3
        pointer = json.loads((store.objects / "daily/1001test/20261010/timeline/.latest.json").read_text())
        self.assertEqual(len(pointer["keys"]), 4)
        self.assertEqual(pointer["save_id"], submitted.json()["save_id"])
        for key in pointer["keys"]:
            self.assertEqual(json.loads((store.objects / key).read_text())["schema_version"], 3)

        with TestClient(self.app, base_url="http://127.0.0.1") as other:
            login = other.post("/api/auth/login", json={"username": "other", "password": "a different long password"})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(other.get(f"/api/projects/{ident}/session/manifest").status_code, 403)
            self.assertEqual(other.post("/api/mock-s3/projects/open", headers={"X-CSRF-Token": login.json()["csrf"]},
                                        json={"path": "daily/1001test/20261010"}).status_code, 403)

    def test_folder_only_selection_does_not_accept_video_uploads(self):
        uploaded = self.client.post("/api/mock-s3/projects", headers=self.csrf,
                                    files=[("files", (self.video.name, self.video.read_bytes(), "video/mp4"))])
        self.assertEqual(uploaded.status_code, 404, uploaded.text)
        self.assertEqual(self.client.get("/api/projects").json(), [])

    def test_authorized_upload_publishes_compressed_fpv_and_timeline(self):
        files = [{"path": f"video/{self.video.name}", "size": self.video.stat().st_size}]
        body = {"collector_name": "张三", "uploader_name": "", "note": "例行采集", "files": files}
        start = self.client.post("/api/mock-s3/uploads", json=body, headers=self.csrf)
        self.assertEqual(start.status_code, 403)
        worker = next(user for user in self.app.state.auth.list_users() if user["username"] == "worker")
        self.app.state.auth.set_upload_permission(worker["id"], True)
        started = self.client.post("/api/mock-s3/uploads", json=body, headers=self.csrf)
        self.assertEqual(started.status_code, 200, started.text)
        task = started.json()
        self.assertEqual(task["prefix"], "daily/20261010/1010张三")
        self.assertEqual(task["uploader_name"], "标注员")
        self.assertEqual(task["uploaded_by_username"], "worker")
        self.assertEqual(self.client.put(f"/api/mock-s3/uploads/{task['id']}/files/0", content=self.video.read_bytes(),
                                         headers=self.csrf).status_code, 200)
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
        published = json.loads((capture / "manifest.json").read_text())
        self.assertEqual((published["collector_name"], published["uploader_name"], published["note"]),
                         ("张三", "标注员", "例行采集"))
        self.assertEqual(published["uploaded_by_username"], "worker")
        self.assertEqual(self.app.state.service.probe(compressed)["codec"], "h264")
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
        self.assertIn(f"/mock-objects/{quote(task['prefix'])}/FPV/", manifest["videos"][0]["url"])
        self.assertEqual(self.client.get(manifest["videos"][0]["url"]).status_code, 200)
        annotations = project["annotations"]
        annotations["posture"] = [{"id": "p1", "label": "动", "kind": "interval", "start_ms": 0,
                                   "end_ms": project["duration_ms"]}]
        saved = self.client.put(f"/api/projects/{ident}/draft", json={"annotations": annotations, "expected_revision": 0},
                                headers=self.csrf)
        self.assertEqual(saved.status_code, 200, saved.text)
        submitted = self.client.post(f"/api/projects/{ident}/writeback", json={"expected_revision": 1}, headers=self.csrf)
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(len(list((capture / "timeline").glob("*.timeline.json"))), 4)
        self.assertEqual(json.loads((capture / "timeline/posture.timeline.json").read_text())["schema_version"], 3)
        self.assertEqual(self.client.post("/api/mock-s3/uploads", json=body, headers=self.csrf).status_code, 409)
        self.app.state.auth.set_upload_permission(worker["id"], False)
        self.assertEqual(self.client.post("/api/mock-s3/uploads", json={**body,"collector_name":"another"}, headers=self.csrf).status_code, 403)

    def test_upload_rejects_non_video_files(self):
        worker = next(user for user in self.app.state.auth.list_users() if user["username"] == "worker")
        self.app.state.auth.set_upload_permission(worker["id"], True)
        body = {"collector_name": "张三", "files": [{"path": "device.txt", "size": 10}]}
        self.assertEqual(self.client.post("/api/mock-s3/uploads", json=body, headers=self.csrf).status_code, 422)
        body["files"] = [{"path": "IMU/sample.csv", "size": 10}]
        self.assertEqual(self.client.post("/api/mock-s3/uploads", json=body, headers=self.csrf).status_code, 422)

    def test_revoked_uploader_cannot_publish_and_can_discard_staging(self):
        worker = next(user for user in self.app.state.auth.list_users() if user["username"] == "worker")
        self.app.state.auth.set_upload_permission(worker["id"], True)
        started = self.client.post("/api/mock-s3/uploads", headers=self.csrf,
                                   json={"collector_name": "another", "uploader_name": "代传人员", "files": [{"path": self.video.name,
                                                                            "size": self.video.stat().st_size}]})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["uploader_name"], "代传人员")
        self.assertIsNone(started.json()["note"])
        ident = started.json()["id"]
        self.assertEqual(self.client.put(f"/api/mock-s3/uploads/{ident}/files/0", content=self.video.read_bytes(),
                                         headers=self.csrf).status_code, 200)
        self.app.state.auth.set_upload_permission(worker["id"], False)
        self.assertEqual(self.client.post(f"/api/mock-s3/uploads/{ident}/finish", headers=self.csrf).status_code, 403)
        self.assertFalse((self.app.state.mock_s3.objects / started.json()["prefix"]).exists())
        self.assertEqual(self.client.request("DELETE", f"/api/mock-s3/uploads/{ident}", headers=self.csrf).status_code, 200)
        self.assertFalse((self.app.state.mock_uploads.staging / ident).exists())
