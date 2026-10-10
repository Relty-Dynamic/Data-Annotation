"""Central visibility for locally sourced annotations without uploading media paths."""
from __future__ import annotations

import io
import json
import os
import unittest
import uuid
import zipfile
from unittest.mock import patch

import httpx

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.auth import AuthStore
from backend.local_reports import LocalReportStore
from backend.cloud_mirror import CloudMirror
from backend import test_service
from backend.testing_annotations import complete_project


class LocalReportTests(unittest.TestCase):
    def setUp(self):
        test_service.PersistenceTests.setUp(self)
        self.nas_root = self.root / "nas-output"
        (self.nas_root / "habit" / "smoking").mkdir(parents=True)

    tearDown = test_service.PersistenceTests.tearDown

    def report(self):
        project = complete_project(self.service.load(self.service.open_path(str(self.source))["id"]))
        snapshot = {key: project[key] for key in ("id", "name", "revision", "duration_ms", "custom_tracks", "annotations")}
        snapshot["videos"] = [{key: video.get(key) for key in ("id", "name", "start_ms", "end_ms", "duration_ms", "recording_start")}
                              for video in project["videos"]]
        _, documents = self.service.export_documents(project, for_writeback=True)
        return snapshot, {axis: value.decode("utf-8") for axis, value in documents.items()}

    def test_owner_admin_visibility_final_documents_and_no_local_paths(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        other = auth.create_user("other", "其他人", "correct horse battery staple")
        admin = auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        store = LocalReportStore(auth, self.nas_root)
        snapshot, documents = self.report()
        saved = store.put(owner, snapshot, None)
        self.assertFalse(saved["has_documents"])
        self.assertFalse(store.put(owner, {**snapshot, "submitted_at": "unverified"}, None)["has_documents"])
        self.assertEqual(store.list(other), [])
        self.assertEqual(store.list(admin)[0]["owner_name"], "本机标注员")
        self.assertNotIn(str(self.source), json.dumps(store.list(admin), ensure_ascii=False))
        store.put(owner, snapshot, documents)
        listing = store.list(admin)[0]
        self.assertTrue(listing["has_documents"])
        self.assertEqual(listing["nas_relative_path"], f'habit/smoking/{snapshot["name"]}-{snapshot["id"]}/timeline')
        self.assertEqual(len(list((self.nas_root / listing["nas_relative_path"]).glob("*.timeline.json"))), 4)
        self.assertEqual((self.nas_root / listing["nas_relative_path"] / "scene.timeline.json").read_text(), documents["scene"])
        store.put(owner, snapshot, documents)  # Retrying the same submission is safe.
        with self.assertRaises(HTTPException) as same_revision_change:
            store.put(owner, {**snapshot, "name": "different content"}, None)
        self.assertEqual(same_revision_change.exception.status_code, 409)
        next_draft = {**snapshot, "revision": snapshot["revision"] + 1, "submitted_at": "unverified"}
        self.assertFalse(store.put(owner, next_draft, None)["has_documents"])
        self.assertFalse(store.list(admin)[0]["has_documents"])
        self.assertTrue(store.put(owner, next_draft, documents)["has_documents"])
        with zipfile.ZipFile(io.BytesIO(store.documents(admin, snapshot["id"]))) as archive:
            self.assertEqual(len(archive.namelist()), 4)
            self.assertEqual(json.loads(archive.read("scene.timeline.json"))["collection_id"], snapshot["id"])
        with self.assertRaises(HTTPException) as forbidden:
            store.put(other, snapshot, documents)
        self.assertEqual(forbidden.exception.status_code, 403)
        with self.assertRaises(HTTPException) as missing:
            store.documents(other, snapshot["id"])
        self.assertEqual(missing.exception.status_code, 404)
        (self.nas_root / listing["nas_relative_path"] / "scene.timeline.json").write_text("external edit")
        with self.assertRaises(HTTPException) as changed:
            store.put(owner, snapshot, documents)
        self.assertEqual(changed.exception.status_code, 409)
        with self.assertRaises(HTTPException) as readback:
            store.documents(admin, snapshot["id"])
        self.assertEqual(readback.exception.status_code, 409)

    def test_stale_revision_and_inconsistent_batch_are_rejected(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        snapshot, documents = self.report()
        snapshot["revision"] = 2
        store.put(owner, snapshot, documents)
        older = {**snapshot, "revision": 1}
        with self.assertRaises(HTTPException) as stale:
            store.put(owner, older, None)
        self.assertEqual(stale.exception.status_code, 409)
        broken = dict(documents)
        scene = json.loads(broken["scene"])
        scene["save_id"] = "different"
        broken["scene"] = json.dumps(scene)
        with self.assertRaises(HTTPException) as mismatch:
            store.put(owner, snapshot, broken)
        self.assertEqual(mismatch.exception.status_code, 422)
        altered = dict(documents)
        posture = json.loads(altered["posture"])
        posture["segments"][0]["label"] = "不匹配"
        altered["posture"] = json.dumps(posture, ensure_ascii=False)
        with self.assertRaises(HTTPException) as content_mismatch:
            store.put(owner, snapshot, altered)
        self.assertEqual(content_mismatch.exception.status_code, 422)

    def test_final_sync_needs_nas_and_does_not_publish_on_failure(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth)
        snapshot, documents = self.report()
        with self.assertRaises(HTTPException) as unavailable:
            store.put(owner, snapshot, documents)
        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertEqual(store.list(owner), [])
        wrong_root = self.root / "empty-nas-root"
        wrong_root.mkdir()
        wrong_store = LocalReportStore(auth, wrong_root)
        with self.assertRaises(HTTPException) as missing_collection:
            wrong_store.put(owner, snapshot, documents)
        self.assertEqual(missing_collection.exception.status_code, 503)
        self.assertFalse((wrong_root / "habit").exists())

    def test_nas_output_symlink_is_rejected(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        snapshot, documents = self.report()
        outside = self.root / "outside"
        outside.mkdir()
        parent = self.nas_root / "habit" / "smoking"
        (parent / f'{snapshot["name"]}-{snapshot["id"]}').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(HTTPException) as unsafe:
            store.put(owner, snapshot, documents)
        self.assertEqual(unsafe.exception.status_code, 409)
        self.assertEqual(list(outside.iterdir()), [])

    def test_interrupted_nas_replacement_restores_previous_batch(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        snapshot, documents = self.report()
        original = store.put(owner, snapshot, documents)
        updated_snapshot = {**snapshot, "name": "更名后的项目", "revision": snapshot["revision"] + 1}
        save_id = uuid.uuid4().hex
        changed = {}
        for axis, value in documents.items():
            item = json.loads(value)
            item["collection_name"] = updated_snapshot["name"]
            item["save_id"] = save_id
            changed[axis] = json.dumps(item, ensure_ascii=False)
        original_replace = os.replace
        replacements = 0
        def fail_second(source, target):
            nonlocal replacements
            replacements += 1
            if replacements == 2:
                raise OSError("fixture interruption")
            return original_replace(source, target)
        with patch("backend.local_reports.os.replace", side_effect=fail_second):
            with self.assertRaises(HTTPException) as interrupted:
                store.put(owner, updated_snapshot, changed)
        self.assertEqual(interrupted.exception.status_code, 503)
        self.assertEqual(store.list(owner)[0]["revision"], snapshot["revision"])
        folder = self.nas_root / original["nas_relative_path"]
        for axis, value in documents.items():
            self.assertEqual((folder / f"{axis}.timeline.json").read_text(), value)

    def test_local_mirror_sends_metadata_then_verified_written_files_without_video_paths(self):
        project = self.service.open_path(str(self.source))
        captured = []
        def handler(request):
            if request.url.path == "/api/health":
                return httpx.Response(200, json={"application": "datamark"})
            if request.url.path == "/api/auth/login":
                return httpx.Response(200, json={"user": {"id": "cloud-user", "role": "annotator"}, "csrf": "csrf"},
                                      headers={"set-cookie": "datamark_session=example; Path=/"})
            if request.url.path == "/api/local-reports":
                self.assertEqual(request.headers["X-CSRF-Token"], "csrf")
                body = json.loads(request.content)
                captured.append(body)
                if body["documents"] and len(captured) == 2:
                    return httpx.Response(200, json={"id": body["snapshot"]["id"], "revision": body["snapshot"]["revision"]})
                return httpx.Response(200, json={"id": body["snapshot"]["id"], "revision": body["snapshot"]["revision"],
                                                 "has_documents": body["documents"] is not None,
                                                 "nas_relative_path": f'{body["snapshot"]["id"]}/timeline' if body["documents"] else None})
            return httpx.Response(404)
        client_type = httpx.Client
        with patch.dict(os.environ, {"DATAMARK_CLOUD_API_ORIGIN": "https://api.example.test:10443"}), \
                patch("backend.cloud_mirror.httpx.Client", side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw)):
            mirror = CloudMirror()
            self.assertEqual(mirror.origin, "https://api.example.test:10443")
            mirror.connect("local-user", "owner", "password")
            mirror.sync("local-user", self.service.load(project["id"]), self.service)
            completed = complete_project(self.service.load(project["id"]))
            self.service.save(completed)
            self.service.writeback(project["id"])
            with self.assertRaises(HTTPException) as old_server:
                mirror.sync("local-user", self.service.load(project["id"]), self.service, final=True)
            self.assertEqual(old_server.exception.status_code, 409)
            mirror.sync("local-user", self.service.load(project["id"]), self.service, final=True)
            mirror.disconnect("local-user")
        self.assertIsNone(captured[0]["documents"])
        self.assertEqual(len(captured[1]["documents"]), 4)
        self.assertEqual(len(captured[2]["documents"]), 4)
        self.assertNotIn(str(self.source), json.dumps(captured, ensure_ascii=False))

    def test_authenticated_admin_can_list_and_export_local_result(self):
        auth = AuthStore(self.root)
        auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        auth.create_user("owner", "本机标注员", "correct horse battery staple")
        auth.create_user("other", "其他标注员", "correct horse battery staple")
        snapshot, documents = self.report()
        with patch.dict(os.environ, {"DATAMARK_ORIGIN": "https://10.20.30.40"}):
            app = create_app(self.root, source_root=self.nas_root)
            with TestClient(app, base_url="https://10.20.30.40") as client:
                self.assertEqual(client.get("/api/local-reports").status_code, 401)
                owner = client.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery staple"})
                saved = client.put("/api/local-reports", json={"snapshot": snapshot, "documents": documents},
                                   headers={"X-CSRF-Token": owner.json()["csrf"]})
                self.assertEqual(saved.status_code, 200, saved.text)
                other = client.post("/api/auth/login", json={"username": "other", "password": "correct horse battery staple"})
                self.assertEqual(client.get("/api/local-reports").json(), [])
                self.assertEqual(client.get(f'/api/local-reports/{snapshot["id"]}/export').status_code, 404)
                self.assertEqual(other.status_code, 200)
                admin = client.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(admin.status_code, 200)
                self.assertEqual(client.get("/api/local-reports").json()[0]["id"], snapshot["id"])
                response = client.get(f'/api/local-reports/{snapshot["id"]}/export')
                self.assertEqual(response.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    self.assertEqual(len(archive.namelist()), 4)
