"""Library ownership, project snapshots, non-destructive selection and JSON restore."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from backend import test_auth, test_track_customization
from backend.auth import AuthStore
from fastapi import HTTPException
from backend.service import AXES, DEFAULT_TRACK_LABELS, LEGACY_TRACK_LABELS, ProjectService
from backend.track_library import TrackLibrary


class LibraryApiTests(unittest.TestCase):
    setUp = test_auth.AccountAccessTests.setUp
    tearDown = test_auth.AccountAccessTests.tearDown
    login = test_auth.AccountAccessTests.login

    def worker(self):
        self.auth.assign(self.project["id"], self.annotator["id"])
        return {"X-CSRF-Token": self.login("worker", "another long safe password")}

    def test_retired_habit_is_hidden_and_cannot_be_enabled_but_custom_events_work(self):
        with self.auth.connection() as con:
            con.execute("INSERT INTO track_library VALUES ('*','habit','旧习惯','event','[\"喝水\"]')")
        headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        self.assertNotIn("habit", [track["id"] for track in self.client.get("/api/account/track-library").json()])
        body = {"id": "habit", "name": "习惯", "mode": "event", "labels": ["喝水"], "is_default": True}
        self.assertEqual(self.client.post("/api/account/track-library", json=body, headers=headers).status_code, 410)
        self.assertEqual(self.client.put("/api/account/track-library/habit", json=body, headers=headers).status_code, 410)
        self.assertEqual(self.client.post("/api/account/track-library/habit/default", json={"is_default": False}, headers=headers).status_code, 410)
        headers = self.worker()
        project = self.app.state.service.load(self.project["id"])
        historical = {"id": "legacy", "label": "喝水", "kind": "point", "lane": 0, "start_ms": 200, "end_ms": 200}
        project["annotations"]["habit"] = [historical]
        self.app.state.service.save(project)
        path = f'/api/projects/{project["id"]}/track-selection'
        self.assertEqual(self.client.put(path, json={"tracks": ["habit"], "expected_revision": project["revision"]}, headers=headers).status_code, 422)
        custom = {"id": "food", "name": "饮食事件", "mode": "event", "labels": ["喝水"]}
        self.assertEqual(self.client.post("/api/account/track-library", json=custom, headers=headers).status_code, 200)
        selected = self.client.put(path, json={"tracks": ["food"], "expected_revision": project["revision"]}, headers=headers)
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(selected.json()["annotations"]["habit"], [historical])
        self.assertEqual(selected.json()["custom_tracks"][0]["mode"], "event")
        with self.auth.connection() as con:
            self.assertIsNotNone(con.execute("SELECT 1 FROM track_library WHERE id='habit'").fetchone())

    def test_shared_defaults_permissions_and_personal_isolation(self):
        headers = self.worker()
        defaults = self.client.get("/api/account/track-library").json()
        self.assertEqual([track["id"] for track in defaults], ["scene", "posture", "category"])
        self.assertEqual(defaults[0]["labels"], DEFAULT_TRACK_LABELS["scene"])
        self.assertEqual(self.client.get("/api/account/track-library").headers["Cache-Control"], "no-store")
        edit = {**defaults[0], "labels": ["管理员新标签"]}
        self.assertEqual(self.client.put("/api/account/track-library/scene", json=edit, headers=headers).status_code, 403)
        self.assertEqual(self.client.request("DELETE", "/api/account/track-library/scene?default=true", json={"confirmed": True}, headers=headers).status_code, 403)
        personal = {"id": "food", "name": "饮食状态", "labels": ["进食", "未进食"]}
        self.assertEqual(self.client.post("/api/account/track-library", json=personal).status_code, 403)
        self.assertEqual(self.client.post("/api/account/track-library", json=personal, headers=headers).status_code, 200)
        admin_headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        self.assertNotIn("food", [track["id"] for track in self.client.get("/api/account/track-library").json()])
        self.assertEqual(self.client.put("/api/account/track-library/scene", json=edit, headers=admin_headers).status_code, 200)
        self.worker()
        self.assertEqual(self.client.get("/api/account/track-library").json()[0]["labels"], ["管理员新标签"])

    def test_get_legacy_project_adopts_library_without_pending_marker(self):
        self.worker()
        library = TrackLibrary(self.auth)
        library.save(self.admin["id"], "category", "活动分类", "state", ["新分类"], default=True)
        raw = self.app.state.service.load(self.project["id"])
        self.assertNotIn("_library_pending", raw)
        raw["fixed_tracks"] = ["scene", "posture"]
        raw["track_labels"] = copy.deepcopy(LEGACY_TRACK_LABELS)
        self.app.state.service.save(raw)
        path = f"/api/projects/{raw['id']}"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()
        self.assertEqual(project["track_labels"]["posture"], DEFAULT_TRACK_LABELS["posture"])
        self.assertEqual(project["track_labels"]["category"], ["新分类"])
        self.assertEqual(project["track_names"]["category"], "活动分类")
        self.assertEqual(project["selected_tracks"], ["scene", "posture", "category"])
        self.assertEqual(project["annotations"]["category"], [])
        self.assertEqual(self.client.get(path).json(), project)
        library.save(self.admin["id"], "category", "再次修改", "state", ["后来标签"], default=True)
        self.assertEqual(self.client.get(path).json(), project)

    def test_personal_update_delete_do_not_change_existing_project_or_annotations(self):
        headers = self.worker()
        library = {"id": "food", "name": "饮食状态", "labels": ["进食", "未进食"]}
        self.assertEqual(self.client.post("/api/account/track-library", json=library, headers=headers).status_code, 200)
        path = f"/api/projects/{self.project['id']}"
        project = self.client.get(path).json()
        selected = self.client.put(path + "/track-selection", json={"tracks": ["scene", "food"], "expected_revision": project["revision"]}, headers=headers)
        self.assertEqual(selected.status_code, 200, selected.text)
        project = selected.json()
        project["annotations"]["food"] = [{"id": "meal", "label": "进食", "start_ms": 100, "end_ms": 300}]
        project = self.client.put(path + "/draft", json={"annotations": project["annotations"], "expected_revision": project["revision"]}, headers=headers).json()
        self.assertEqual(self.client.put("/api/account/track-library/food", json={**library, "name": "新版饮食", "labels": ["吃饭"]}, headers=headers).status_code, 200)
        self.assertEqual(self.client.request("DELETE", "/api/account/track-library/food", json={"confirmed": True}, headers=headers).status_code, 200)
        kept = self.client.get(path).json()
        self.assertEqual(kept["custom_tracks"], project["custom_tracks"])
        self.assertEqual(kept["annotations"], project["annotations"])
        hidden = self.client.put(path + "/track-selection", json={"tracks": [], "expected_revision": kept["revision"]}, headers=headers).json()
        self.assertEqual(hidden["annotations"], kept["annotations"])
        self.assertEqual(self.client.put(path + "/track-selection", json={"tracks": ["food"], "expected_revision": kept["revision"]}, headers=headers).status_code, 409)
        restored = self.client.put(path + "/track-selection", json={"tracks": ["food"], "expected_revision": hidden["revision"]}, headers=headers).json()
        self.assertEqual(restored["annotations"], kept["annotations"])
        operations = self.client.get(path + "/operations").json()
        self.assertEqual(operations[0]["after"]["selected_tracks"], ["food"])
        self.assertEqual(operations[0]["actor_name"], "张标注")
        history = self.client.get(path + "/history").json()
        self.assertEqual(history[0]["action"], "create")

    def test_unknown_or_other_accounts_axes_cannot_be_selected(self):
        headers = self.worker()
        path = f"/api/projects/{self.project['id']}/track-selection"
        for ids in (["other_user_axis"], ["scene", "scene"], ["../axis"]):
            response = self.client.put(path, json={"tracks": ids, "expected_revision": 0}, headers=headers)
            self.assertEqual(response.status_code, 422, response.text)

    def test_admin_can_add_remove_defaults_and_removal_survives_restart(self):
        headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        track = {"id": "activity", "name": "活动", "labels": ["活动中", "未活动"], "is_default": True}
        self.assertEqual(self.client.post("/api/account/track-library", json=track, headers=headers).status_code, 200)
        self.assertEqual(self.client.request("DELETE", "/api/account/track-library/category?default=true", json={"confirmed": True}, headers=headers).status_code, 200)
        restarted = TrackLibrary(AuthStore(self.root))
        self.assertEqual([track["id"] for track in restarted.defaults()], ["scene", "posture", "activity"])

    def test_default_toggle_requires_admin_and_csrf(self):
        headers = self.worker()
        path = "/api/account/track-library/scene/default"
        for value in (True, False):
            self.assertEqual(self.client.post(path, json={"is_default": value}, headers=headers).status_code, 403)
        self.login("admin", "correct horse battery staple")
        self.assertEqual(self.client.post(path, json={"is_default": False}).status_code, 403)
        self.assertTrue(self.client.get("/api/account/track-library").json()[0]["is_default"])

    def test_default_toggle_preserves_definition_project_and_restart(self):
        headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        path = "/api/account/track-library/scene/default"
        original = self.client.get("/api/account/track-library").json()[0]
        project = self.client.get(f"/api/projects/{self.project['id']}").json()
        response = self.client.post(path, json={"is_default": False}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {**original, "is_default": False})
        restarted = TrackLibrary(AuthStore(self.root))
        self.assertNotIn("scene", [track["id"] for track in restarted.defaults()])
        self.assertIn({**original, "is_default": False}, restarted.list(self.admin["id"]))
        self.worker()
        self.assertNotIn("scene", [track["id"] for track in self.client.get("/api/account/track-library").json()])
        headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        # A removed built-in axis remains editable in its owner's personal library.
        edit = {**original, "is_default": False, "name": "新场景"}
        self.assertEqual(self.client.put("/api/account/track-library/scene", json=edit, headers=headers).status_code, 200)
        response = self.client.post(path, json={"is_default": True}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {**edit, "is_default": True})
        self.assertEqual(self.client.get(f"/api/projects/{self.project['id']}").json(), project)
        self.worker()
        self.assertIn({**edit, "is_default": True}, self.client.get("/api/account/track-library").json())
        with self.auth.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM track_library_events WHERE track_id='scene'").fetchone()[0], 5)

    def test_promote_conflict_keeps_both_personal_libraries(self):
        worker_headers = self.worker()
        track = {"id": "food", "name": "饮食", "labels": ["进食"]}
        self.assertEqual(self.client.post("/api/account/track-library", json=track, headers=worker_headers).status_code, 200)
        headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        self.assertEqual(self.client.post("/api/account/track-library", json=track, headers=headers).status_code, 200)
        path = "/api/account/track-library/food/default"
        response = self.client.post(path, json={"is_default": True}, headers=headers)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(next(item for item in self.client.get("/api/account/track-library").json() if item["id"] == "food")["is_default"])
        worker_headers = self.worker()
        self.assertEqual(self.client.request("DELETE", "/api/account/track-library/food", json={"confirmed": True}, headers=worker_headers).status_code, 200)
        headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        self.assertEqual(self.client.post(path, json={"is_default": True}, headers=headers).status_code, 200)
        self.assertEqual(self.client.post(path, json={"is_default": True}, headers=headers).status_code, 404)
        self.assertEqual(self.client.post(path, json={"is_default": False}, headers=headers).status_code, 200)

    def test_promote_default_limit_keeps_source_track(self):
        library = TrackLibrary(self.auth)
        for index in range(16):
            library.save(self.admin["id"], f"axis_{index}", "共享轴", "state", ["标签"], default=True, create=True)
        library.save(self.admin["id"], "food", "饮食", "event", ["进食"], create=True)
        headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        response = self.client.post("/api/account/track-library/food/default", json={"is_default": True}, headers=headers)
        self.assertEqual(response.status_code, 422, response.text)
        track = next(item for item in self.client.get("/api/account/track-library").json() if item["id"] == "food")
        self.assertFalse(track["is_default"])
        self.assertEqual(track["mode"], "event")

    def test_remove_default_personal_limit_keeps_shared_definition(self):
        library = TrackLibrary(self.auth)
        for index in range(32):
            library.save(self.admin["id"], f"axis_{index}", "个人轴", "state", ["标签"], create=True)
        headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        response = self.client.post("/api/account/track-library/scene/default", json={"is_default": False}, headers=headers)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("scene", [track["id"] for track in library.defaults()])

    def test_toggle_preserves_existing_event_mode_and_cannot_take_other_personal_axis(self):
        library = TrackLibrary(self.auth)
        library.save(self.annotator["id"], "private_food", "私有饮食", "event", ["进食"], create=True)
        event = library.save(self.admin["id"], "food", "饮食", "event", ["进食"], create=True)
        headers = {"X-CSRF-Token": self.login("admin", "correct horse battery staple")}
        self.assertEqual(self.client.post("/api/account/track-library/private_food/default", json={"is_default": True}, headers=headers).status_code, 404)
        path = "/api/account/track-library/food/default"
        for default in (True, False, True):
            response = self.client.post(path, json={"is_default": default}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {**event, "is_default": default})


class LibraryPersistenceTests(unittest.TestCase):
    setUp = test_track_customization.TrackCustomizationTests.setUp
    tearDown = test_track_customization.TrackCustomizationTests.tearDown

    def library(self):
        return TrackLibrary(AuthStore(self.root / "app"))

    def initialize(self):
        project = self.service.open_path(str(self.source))
        return self.service.initialize_track_library(project["id"], self.library().defaults())

    def test_defaults_snapshot_applies_to_new_projects_and_does_not_update_on_reopen(self):
        library = self.library()
        library.save(None, "scene", "环境", "state", ["新室内"], default=True)
        project = self.initialize()
        self.assertEqual(project["selected_tracks"], ["scene", "posture", "category"])
        self.assertEqual(project["track_labels"]["scene"], ["新室内"])
        self.assertEqual(project["track_names"]["scene"], "环境")
        library.save(None, "scene", "环境新版", "state", ["新室外"], default=True)
        reopened = self.service.initialize_track_library(project["id"], library.defaults())
        self.assertEqual(reopened, project)

    def test_hidden_axis_annotations_and_selection_round_trip_json_and_database(self):
        library = self.library()
        project = self.initialize()
        project["annotations"]["scene"] = [{"id": "scene", "label": "室内", "start_ms": 0, "end_ms": 1000}]
        project = self.service.update_draft(project["id"], project["annotations"], project["revision"])
        project = self.service.select_library_tracks(project["id"], [], library.list(None), project["revision"])
        # Coverage for hidden scene/posture is not required; hidden data is exported.
        _, docs = self.service.export_documents(self.service.load(project["id"]))
        self.assertEqual(json.loads(docs["scene"])["timebase"]["selected_tracks"], [])
        self.assertEqual(json.loads(docs["scene"])["segments"][0]["label"], "室内")
        # A full ZIP export still preserves hidden axes and an empty selection.
        destination = self.source / "timeline"
        destination.mkdir()
        for axis, payload in docs.items():
            (destination / f"{axis}.timeline.json").write_bytes(payload)
        fresh = ProjectService(self.root / "reopened-app")
        fresh.probe = self.service.probe
        try:
            reopened = fresh.open_path(str(self.source))
            reopened = fresh.initialize_track_library(reopened["id"], library.defaults())
            self.assertEqual(reopened["selected_tracks"], [])
            self.assertEqual(reopened["annotations"], project["annotations"])
            restored = fresh.select_library_tracks(reopened["id"], ["scene"], library.list(None), reopened["revision"])
            self.assertEqual(restored["annotations"], project["annotations"])
        finally:
            fresh.sessions.close()
            fresh.previews.close()
        with self.service.connection() as con:
            count = con.execute("SELECT COUNT(*) FROM project_operations WHERE project_id=?", (project["id"],)).fetchone()[0]
        self.assertGreaterEqual(count, 3)

    def test_posture_only_writeback_and_fresh_reopen(self):
        project = self.initialize()
        annotations = project["annotations"]
        annotations["scene"] = [{"id": "hidden", "label": "室内", "start_ms": 0, "end_ms": 1000}]
        annotations["posture"] = [{"id": "posture", "label": "坐", "start_ms": 0, "end_ms": 1000}]
        project = self.service.update_draft(project["id"], annotations, project["revision"])
        project = self.service.select_library_tracks(project["id"], ["posture"], self.library().list(None), project["revision"])
        result = self.service.writeback(project["id"])
        self.assertEqual([Path(path).name for path in result["paths"]], ["posture.timeline.json"])
        self.assertEqual({path.name for path in (self.source / "timeline").glob("*.timeline.json")}, {"posture.timeline.json"})
        self.assertEqual(self.service.load(project["id"])["annotations"], project["annotations"])
        self.assertEqual(set(self.service.export_documents(self.service.load(project["id"]))[1]), set(AXES))
        fresh = ProjectService(self.root / "posture-reopened")
        fresh.probe = self.service.probe
        try:
            reopened = fresh.open_path(str(self.source))
            self.assertEqual(reopened["selected_tracks"], ["posture"])
            self.assertEqual(reopened["annotations"]["posture"], project["annotations"]["posture"])
        finally:
            fresh.sessions.close()
            fresh.previews.close()

    def test_selection_changes_leave_unselected_files_untouched(self):
        project = self.initialize()
        self.service.writeback(project["id"])
        folder = self.source / "timeline"
        preserved = {axis: (folder / f"{axis}.timeline.json").read_bytes() for axis in ("scene", "category")}
        project = self.service.select_library_tracks(project["id"], ["posture"], self.library().list(None), project["revision"])
        self.service.writeback(project["id"])
        for axis, payload in preserved.items():
            self.assertEqual((folder / f"{axis}.timeline.json").read_bytes(), payload)
        restored, _ = self.service.read_external(self.service.load(project["id"]))
        self.assertEqual(restored["posture"], [])
        self.service.writeback(project["id"])  # Retained files are included in conflict hashes.

    def test_custom_only_writeback_keeps_hidden_open_events_in_draft(self):
        project = self.initialize()
        project = self.service.add_custom_track(project["id"], "饮食", "event", [], project["revision"], json_name="food")
        annotations = project["annotations"]
        annotations["food"] = [{"id": "open", "label": "进食", "start_ms": 0, "end_ms": None}]
        project = self.service.update_draft(project["id"], annotations, project["revision"])
        raw = self.service.load(project["id"])
        raw["selected_tracks"] = ["posture"]
        self.service.save(raw)
        self.service.writeback(project["id"])
        self.assertIsNone(self.service.load(project["id"])["annotations"]["food"][0]["end_ms"])
        raw = self.service.load(project["id"])
        raw["selected_tracks"] = ["food"]
        self.service.save(raw)
        with self.assertRaises(HTTPException):
            self.service.writeback(project["id"])
        raw["annotations"]["food"][0]["end_ms"] = 1000
        self.service.save(raw)
        result = self.service.writeback(project["id"])
        self.assertEqual(result["paths"], [str(self.source / "timeline" / "food.timeline.json")])
        restored, _ = self.service.read_external(self.service.load(project["id"]))
        self.assertEqual(restored["food"][0]["label"], "进食")

    def test_no_selection_rejects_writeback_without_creating_files(self):
        project = self.initialize()
        project = self.service.select_library_tracks(project["id"], [], self.library().list(None), project["revision"])
        with self.assertRaises(HTTPException) as error:
            self.service.writeback(project["id"])
        self.assertEqual(error.exception.status_code, 422)
        self.assertFalse((self.source / "timeline").exists())
        self.assertIsNone(self.service.load(project["id"]).get("last_writeback"))

    def test_missing_selected_file_still_rejects_partial_batch(self):
        project = self.initialize()
        self.service.writeback(project["id"])
        (self.source / "timeline" / "posture.timeline.json").unlink()
        with self.assertRaises(HTTPException):
            self.service.read_external(self.service.load(project["id"]))

    def test_legacy_buttons_adopt_library_and_old_annotation_notes_round_trip(self):
        project = self.service.open_path(str(self.source))
        raw = self.service.load(project["id"])
        raw.pop("_library_pending", None)
        raw["fixed_tracks"] = list(AXES)
        raw["track_labels"] = copy.deepcopy(LEGACY_TRACK_LABELS)
        raw["annotations"]["scene"] = [{"id": "car", "label": "车内", "start_ms": 0, "end_ms": 1000, "lane": 0}]
        raw["annotations"]["posture"] = [{"id": "moving", "label": "动（走廊）", "start_ms": 0, "end_ms": 1000, "lane": 0}]
        raw["annotations"]["category"] = [
            {"id": "focus", "label": "专注（阅读）", "start_ms": 0, "end_ms": 700, "lane": 0, "mode": "state"},
            {"id": "activity", "label": "活动", "start_ms": 100, "end_ms": 300, "lane": 1, "mode": "state"},
        ]
        self.service.save(raw)
        original = self.service.public(raw)["annotations"]
        library = self.library()
        initialized = self.service.initialize_track_library(raw["id"], library.defaults())
        self.assertEqual(initialized["track_labels"], DEFAULT_TRACK_LABELS)
        self.assertEqual(initialized["annotations"], original)
        saved = self.service.update_draft(raw["id"], original, initialized["revision"])
        self.service.writeback(raw["id"])
        fresh = ProjectService(self.root / "legacy-reopened-app")
        fresh.probe = self.service.probe
        try:
            library.save(None, "posture", "之后的姿势", "state", ["新姿势"], default=True)
            reopened = fresh.open_path(str(self.source))
            reopened = fresh.initialize_track_library(reopened["id"], library.defaults())
            self.assertEqual(reopened["track_labels"], initialized["track_labels"])
            self.assertEqual(reopened["track_names"], initialized["track_names"])
            self.assertEqual(reopened["annotations"], saved["annotations"])
        finally:
            fresh.sessions.close()
            fresh.previews.close()

    def test_partly_initialized_legacy_project_preserves_hidden_selection(self):
        project = self.service.open_path(str(self.source))
        raw = self.service.load(project["id"])
        raw.pop("_library_pending", None)
        raw.update(_library_initialized=True, selected_tracks=[], track_labels=copy.deepcopy(LEGACY_TRACK_LABELS))
        raw["annotations"]["posture"] = [{"id": "moving", "label": "动", "start_ms": 0, "end_ms": 1000, "lane": 2}]
        self.service.save(raw)
        original = self.service.public(raw)["annotations"]
        library = self.library()
        upgraded = self.service.initialize_track_library(raw["id"], library.defaults())
        self.assertEqual(upgraded["selected_tracks"], [])
        self.assertEqual(upgraded["track_labels"], DEFAULT_TRACK_LABELS)
        self.assertEqual(upgraded["annotations"], original)
        self.assertTrue(upgraded["draft_dirty"])
        self.assertEqual(upgraded["revision"], raw["revision"] + 1)
        self.assertEqual(self.service.initialize_track_library(raw["id"], library.defaults()), upgraded)

    def test_legacy_upgrade_preserves_customized_catalogs_and_named_snapshots(self):
        project = self.service.open_path(str(self.source))
        raw = self.service.load(project["id"])
        raw.pop("_library_pending", None)
        raw.update(_library_initialized=True, selected_tracks=["scene"], track_labels=copy.deepcopy(LEGACY_TRACK_LABELS))
        raw["track_labels"]["scene"] = ["用户自定义场景"]
        raw["track_names"] = {"posture": "已保存的姿势"}
        raw["custom_tracks"] = [{"id": "food", "name": "原饮食", "mode": "state", "labels": ["吃饭"]}]
        raw["annotations"]["food"] = []
        self.service.save(raw)
        upgraded = self.service.initialize_track_library(raw["id"], self.library().defaults())
        self.assertEqual(upgraded["track_labels"]["scene"], ["用户自定义场景"])
        self.assertEqual(upgraded["track_labels"]["posture"], LEGACY_TRACK_LABELS["posture"])
        self.assertEqual(upgraded["track_names"]["posture"], "已保存的姿势")
        self.assertEqual(upgraded["track_labels"]["category"], DEFAULT_TRACK_LABELS["category"])
        self.assertEqual(upgraded["custom_tracks"], raw["custom_tracks"])
        self.assertEqual(upgraded["selected_tracks"], ["scene"])

    def test_hidden_legacy_project_keeps_all_axes_hidden_during_initial_adoption(self):
        project = self.service.open_path(str(self.source))
        raw = self.service.load(project["id"])
        raw.pop("_library_pending", None)
        raw.update(fixed_tracks=["scene", "posture"], selected_tracks=[], track_labels=copy.deepcopy(LEGACY_TRACK_LABELS))
        self.service.save(raw)
        upgraded = self.service.initialize_track_library(raw["id"], self.library().defaults())
        self.assertEqual(upgraded["fixed_tracks"], ["scene", "posture", "category"])
        self.assertEqual(upgraded["selected_tracks"], [])
        self.assertEqual(upgraded["annotations"], project["annotations"])

    def test_legacy_project_at_custom_axis_limit_still_opens_and_keeps_selection(self):
        project = self.service.open_path(str(self.source))
        raw = self.service.load(project["id"])
        raw.pop("_library_pending", None)
        raw["track_labels"] = copy.deepcopy(LEGACY_TRACK_LABELS)
        raw["custom_tracks"] = [{"id": f"axis_{index}", "name": f"原轴 {index}", "mode": "state", "labels": ["原标签"]}
                                for index in range(16)]
        raw["selected_tracks"] = ["axis_15", "scene"]
        raw["annotations"].update({track["id"]: [] for track in raw["custom_tracks"]})
        raw["annotations"]["axis_15"] = [{"id": "old", "label": "原标签", "start_ms": 100, "end_ms": 300, "lane": 0}]
        self.service.save(raw)
        defaults = [*self.library().defaults(),
                    {"id": "axis_0", "name": "库中同名轴", "mode": "state", "labels": ["新标签"]},
                    {"id": "added_default", "name": "新增默认轴", "mode": "state", "labels": ["默认标签"]}]
        upgraded = self.service.initialize_track_library(raw["id"], defaults)
        self.assertEqual(upgraded["custom_tracks"], raw["custom_tracks"])
        self.assertEqual(upgraded["selected_tracks"], raw["selected_tracks"])
        self.assertEqual(upgraded["annotations"], self.service.public(raw)["annotations"])
        self.assertEqual(upgraded["track_labels"], DEFAULT_TRACK_LABELS)
        self.assertEqual(self.service.initialize_track_library(raw["id"], defaults), upgraded)

    def test_axis_absent_from_defaults_is_frozen_before_it_becomes_default_again(self):
        library = self.library()
        library.delete(None, "category", default=True)
        project = self.service.open_path(str(self.source))
        raw = self.service.load(project["id"])
        raw.pop("_library_pending", None)
        raw.update(_library_initialized=True, selected_tracks=["category"], track_labels=copy.deepcopy(LEGACY_TRACK_LABELS))
        self.service.save(raw)
        upgraded = self.service.initialize_track_library(raw["id"], library.defaults())
        self.assertEqual(upgraded["track_names"]["category"], "大类")
        self.assertEqual(upgraded["track_labels"]["category"], LEGACY_TRACK_LABELS["category"])
        self.assertEqual(upgraded["selected_tracks"], ["category"])
        library.save(None, "category", "重新添加的默认分类", "state", ["后来标签"], default=True, create=True)
        self.assertEqual(self.service.initialize_track_library(raw["id"], library.defaults()), upgraded)

    def test_legacy_project_adds_only_defaults_that_fit_remaining_custom_slots(self):
        project = self.service.open_path(str(self.source))
        raw = self.service.load(project["id"])
        raw.pop("_library_pending", None)
        raw["track_labels"] = copy.deepcopy(LEGACY_TRACK_LABELS)
        raw["custom_tracks"] = [{"id": f"axis_{index}", "name": f"原轴 {index}", "mode": "state", "labels": ["原标签"]}
                                for index in range(15)]
        raw["annotations"].update({track["id"]: [] for track in raw["custom_tracks"]})
        self.service.save(raw)
        defaults = [*self.library().defaults(),
                    {"id": "first_default", "name": "第一个默认轴", "mode": "state", "labels": ["默认标签"]},
                    {"id": "second_default", "name": "第二个默认轴", "mode": "state", "labels": ["默认标签"]}]
        upgraded = self.service.initialize_track_library(raw["id"], defaults)
        self.assertEqual(upgraded["custom_tracks"][:-1], raw["custom_tracks"])
        self.assertEqual(upgraded["custom_tracks"][-1]["id"], "first_default")
        self.assertEqual(upgraded["selected_tracks"], ["scene", "posture", "category", "first_default",
                                                    *[track["id"] for track in raw["custom_tracks"]]])
        self.assertNotIn("second_default", upgraded["annotations"])
        self.assertEqual(self.service.initialize_track_library(raw["id"], defaults), upgraded)


if __name__ == "__main__":
    unittest.main()
