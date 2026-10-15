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
from backend.service import AXES, FILENAMES, digest
from backend import test_service
from backend.testing_annotations import complete_project


class LocalReportTests(unittest.TestCase):
    def setUp(self):
        test_service.PersistenceTests.setUp(self)
        self.nas_root = self.root / "nas-output"
        (self.nas_root / "habit" / "smoking").mkdir(parents=True)

    tearDown = test_service.PersistenceTests.tearDown

    def legacy_written_project(self, *, written_tracks=True):
        project = complete_project(self.service.load(self.service.open_path(str(self.source))["id"]))
        save_id, payloads = self.service.export_documents(project)
        folder = self.source / "timeline"
        folder.mkdir(exist_ok=True)
        for axis, payload in payloads.items():
            document = json.loads(payload)
            if written_tracks:
                document["timebase"]["written_tracks"] = list(AXES)
            (folder / FILENAMES[axis]).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        project["last_writeback"] = {"save_id": save_id, "saved_at": document["saved_at"]}
        project["draft_dirty"] = False
        project["_external_hashes"] = self.service.current_external_hashes(self.source)
        self.service.save(project)
        return self.service.load(project["id"])

    def sync_to_nas(self, project, store, owner, **options):
        captured = []

        def handler(request):
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json=store.put(owner, body["snapshot"], body["documents"]))

        mirror = CloudMirror()
        client = httpx.Client(base_url="https://api.example.test:10443", transport=httpx.MockTransport(handler))
        mirror.sessions["local-user"] = (client, owner, "csrf")
        try:
            result = mirror.sync("local-user", project, self.service, **options)
        finally:
            mirror.disconnect("local-user")
        return result, captured

    def report(self):
        project = complete_project(self.service.load(self.service.open_path(str(self.source))["id"]))
        snapshot = {key: project[key] for key in ("id", "name", "revision", "duration_ms", "custom_tracks", "annotations")}
        snapshot["videos"] = [{key: video.get(key) for key in ("id", "name", "start_ms", "end_ms", "duration_ms", "recording_start")}
                              for video in project["videos"]]
        # This fixture represents the four-file format emitted by older clients.
        _, documents = self.service.export_documents(project)
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

    def test_local_project_with_removed_posture_does_not_write_posture_json_to_nas(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        project = complete_project(self.service.load(self.service.open_path(str(self.source))["id"]))
        project["fixed_tracks"] = ["scene", "category", "habit"]
        project["annotations"]["posture"] = []
        snapshot = {key: project[key] for key in ("id", "name", "revision", "duration_ms", "fixed_tracks", "custom_tracks", "annotations") if key in project}
        snapshot["videos"] = [{key: video.get(key) for key in ("id", "name", "start_ms", "end_ms", "duration_ms", "recording_start")}
                              for video in project["videos"]]
        _, raw = self.service.export_documents(project)
        result = store.put(owner, snapshot, {axis: value.decode("utf-8") for axis, value in raw.items() if axis in project["fixed_tracks"]})
        folder = self.nas_root / result["nas_relative_path"]
        self.assertFalse((folder / "posture.timeline.json").exists())
        self.assertEqual({path.name for path in folder.glob("*.timeline.json")},
                         {"scene.timeline.json", "category.timeline.json", "habit.timeline.json"})

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

    def test_selected_only_nas_submission_preserves_previous_unselected_files(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        snapshot, documents = self.report()
        first = store.put(owner, snapshot, documents)
        folder = self.nas_root / first["nas_relative_path"]
        preserved = {axis: (folder / f"{axis}.timeline.json").read_bytes() for axis in ("scene", "category", "habit")}
        snapshot = {**snapshot, "revision": snapshot["revision"] + 1, "selected_tracks": ["posture"]}
        snapshot["annotations"]["habit"] = [{"id": "ongoing", "label": "吸烟", "start_ms": 0, "end_ms": None}]
        posture = json.loads(documents["posture"])
        posture["save_id"] = uuid.uuid4().hex
        posture["timebase"]["selected_tracks"] = ["posture"]
        posture["timebase"]["written_tracks"] = ["posture"]
        documents = {"posture": json.dumps(posture, ensure_ascii=False)}
        store.put(owner, snapshot, documents)
        store.put(owner, snapshot, documents)
        for axis, payload in preserved.items():
            self.assertEqual((folder / f"{axis}.timeline.json").read_bytes(), payload)
        with zipfile.ZipFile(io.BytesIO(store.documents(owner, snapshot["id"]))) as archive:
            self.assertEqual(archive.namelist(), ["posture.timeline.json"])
        self.assertIsNone(store.list(owner)[0]["annotations"]["habit"][0]["end_ms"])

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
        try:
            (parent / f'{snapshot["name"]}-{snapshot["id"]}').symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows does not grant this test process symlink privileges")
            raise
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

    def test_local_mirror_has_no_retired_public_nas_default(self):
        with patch.dict(os.environ, {"DATAMARK_CLOUD_API_ORIGIN": ""}):
            mirror = CloudMirror()
        self.assertEqual(mirror.status("local-user")["configured"], False)

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
            completed["selected_tracks"] = ["posture"]
            self.service.save(completed)
            self.service.writeback(project["id"])
            with self.assertRaises(HTTPException) as old_server:
                mirror.sync("local-user", self.service.load(project["id"]), self.service, final=True)
            self.assertEqual(old_server.exception.status_code, 409)
            mirror.sync("local-user", self.service.load(project["id"]), self.service, final=True)
            mirror.disconnect("local-user")
        self.assertIsNone(captured[0]["documents"])
        self.assertEqual(set(captured[1]["documents"]), {"posture"})
        self.assertEqual(set(captured[2]["documents"]), {"posture"})
        self.assertNotIn(str(self.source), json.dumps(captured, ensure_ascii=False))

    def test_final_sync_uses_archived_batch_and_preserves_retained_older_habit(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        project = self.legacy_written_project()
        first, captured = self.sync_to_nas(project, store, owner, final=True)
        self.assertEqual(captured[0]["snapshot"]["selected_tracks"], list(AXES))
        self.assertEqual(set(captured[0]["documents"]), set(AXES))
        folder = self.nas_root / first["nas_relative_path"]
        habit_before = (folder / "habit.timeline.json").read_bytes()
        source_habit = (self.source / "timeline" / "habit.timeline.json").read_bytes()
        self.service.writeback(project["id"])
        project = self.service.load(project["id"])
        second, captured = self.sync_to_nas(project, store, owner, final=True)
        self.assertEqual(second["nas_relative_path"], first["nas_relative_path"])
        self.assertEqual(captured[0]["snapshot"]["selected_tracks"], ["scene", "posture", "category"])
        self.assertEqual(set(captured[0]["documents"]), {"scene", "posture", "category"})
        self.assertEqual((folder / "habit.timeline.json").read_bytes(), habit_before)
        self.assertEqual((self.source / "timeline" / "habit.timeline.json").read_bytes(), source_habit)

    def test_final_sync_rejects_unwritten_draft_without_creating_output(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        project = complete_project(self.service.load(self.service.open_path(str(self.source))["id"]))
        project["draft_dirty"] = True
        self.service.save(project)
        before = self.service.load(project["id"])
        with self.assertRaises(HTTPException) as unwritten:
            self.sync_to_nas(project, store, owner, final=True)
        self.assertEqual(unwritten.exception.status_code, 409)
        self.assertEqual(store.list(owner), [])
        self.assertFalse((self.source / "timeline").exists())
        self.assertEqual(self.service.load(project["id"]), before)

    def test_same_revision_source_folder_changes_remain_rejected(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        snapshot, documents = self.report()
        changed = {**snapshot, "source_folder_name": "其他采集目录"}
        store.put(owner, snapshot, None)
        for incoming_documents in (None, documents):
            with self.subTest(final=incoming_documents is not None), self.assertRaises(HTTPException) as conflict:
                store.put(owner, changed, incoming_documents)
            self.assertEqual(conflict.exception.status_code, 409)
        written = store.put(owner, snapshot, documents)
        with self.assertRaises(HTTPException) as conflict:
            store.put(owner, changed, documents)
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(store.list(owner)[0]["nas_relative_path"], written["nas_relative_path"])

    def test_final_sync_legacy_axis_fallback_rejects_incomplete_saved_batch(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        project = self.legacy_written_project(written_tracks=False)
        result, captured = self.sync_to_nas(project, store, owner, final=True)
        self.assertEqual(set(captured[0]["documents"]), set(AXES))
        output = self.nas_root / result["nas_relative_path"]
        before = {path.name: path.read_bytes() for path in output.glob("*.timeline.json")}
        path = self.source / "timeline" / "habit.timeline.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["save_id"] = "different-batch"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        project["_external_hashes"]["habit"] = digest(path.read_bytes())
        self.service.save(project)
        with self.assertRaises(HTTPException) as mismatch:
            self.sync_to_nas(project, store, owner, final=True)
        self.assertEqual(mismatch.exception.status_code, 409)
        self.assertEqual({path.name: path.read_bytes() for path in output.glob("*.timeline.json")}, before)

    def test_final_sync_preserves_named_custom_event_json(self):
        auth = AuthStore(self.root)
        owner = auth.create_user("owner", "本机标注员", "correct horse battery staple")
        store = LocalReportStore(auth, self.nas_root)
        project = self.service.open_path(str(self.source))
        created = self.service.add_custom_track(project["id"], "饮食", "event", [], project["revision"], json_name="food")
        project = complete_project(self.service.load(created["id"]))
        project["annotations"]["food"] = [{"id": "meal", "label": "午餐", "kind": "point", "lane": 0,
                                            "start_ms": 500, "end_ms": 500}]
        self.service.save(project)
        self.service.writeback(project["id"])
        result, captured = self.sync_to_nas(self.service.load(project["id"]), store, owner, final=True)
        self.assertEqual(set(captured[0]["documents"]), {"scene", "posture", "category", "food"})
        source = self.source / "timeline" / "food.timeline.json"
        destination = self.nas_root / result["nas_relative_path"] / "food.timeline.json"
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(json.loads(destination.read_bytes())["segments"][0]["label"], "午餐")

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
