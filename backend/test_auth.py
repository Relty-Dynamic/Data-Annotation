"""Login, project authorization and server-owned annotation history."""
from __future__ import annotations

import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.auth import AuthStore
from backend.service import empty_annotations, now


class AccountAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="auth-tests-")
        self.root = Path(self.temp.name)
        self.auth = AuthStore(self.root)
        self.admin = self.auth.create_user("admin", "管理员", "correct horse battery staple", "admin")
        self.annotator = self.auth.create_user("worker", "张标注", "another long safe password")
        self.app = create_app(self.root)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.project = {
            "id": "a" * 32, "name": "测试采集", "duration_ms": 1000,
            "videos": [{"id": "v0001", "name": "clip.mp4", "start_ms": 0, "end_ms": 1000, "duration_ms": 1000}],
            "annotations": empty_annotations(), "revision": 0, "updated_at": now(), "_sources": {},
            "warnings": [], "gaps": [], "source_fingerprint": "0" * 64,
        }
        self.app.state.service.save(self.project)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, username, password):
        response = self.client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf"]

    def test_login_csrf_assignment_media_and_revocation(self):
        project_id = self.project["id"]
        self.assertEqual(self.client.get("/api/projects").status_code, 401)
        self.assertEqual(self.client.get(f"/api/media/{project_id}/v0001").status_code, 401)
        csrf = self.login("worker", "another long safe password")
        self.assertEqual(self.client.get("/api/projects").json(), [])
        self.assertEqual(self.client.get(f"/api/projects/{project_id}").status_code, 403)
        self.assertEqual(self.client.get(f"/api/projects/{project_id}/export-documents").status_code, 403)
        self.assertEqual(self.client.get(f"/api/%70rojects/{project_id}").status_code, 403)
        self.assertEqual(self.client.get(f"/api/session-media/{project_id}/v1/v0001").status_code, 403)
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/writeback", headers={"X-CSRF-Token": csrf}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/annotations/reset-from-empty-source", json={"expected_revision": 0}, headers={"X-CSRF-Token": csrf}).status_code, 403)
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.auth.assign(project_id, self.annotator["id"])
        self.assertEqual(len(self.client.get("/api/projects").json()), 1)
        self.assertEqual(self.client.get(f"/api/projects/{project_id}").status_code, 200)
        with patch.object(self.app.state.service, "export_files", return_value={"documents": []}) as export_files:
            self.assertEqual(self.client.get(f"/api/projects/{project_id}/export-documents").status_code, 200)
            export_files.assert_called_once_with(project_id)
        reset_path = f"/api/projects/{project_id}/annotations/reset-from-empty-source"
        self.assertEqual(self.client.post(reset_path, json={"expected_revision": 0}).status_code, 403)
        with patch.object(self.app.state.service, "reset_annotations_from_empty_source", return_value={"ok": True}) as reset:
            self.assertEqual(self.client.post(reset_path, json={"expected_revision": 0}, headers={"X-CSRF-Token": csrf}).status_code, 200)
            reset.assert_called_once_with(project_id, 0)
        with patch.object(self.app.state.service, "writeback", return_value={"ok": True}) as writeback:
            self.assertEqual(self.client.post(f"/api/projects/{project_id}/writeback", headers={"X-CSRF-Token": csrf}).status_code, 200)
            writeback.assert_called_once_with(project_id)
        with patch.object(self.app.state.service, 'clear_local_previews', return_value={'cleared': project_id}) as clear:
            path = f'/api/projects/{project_id}/preview-cache/clear'
            self.assertEqual(self.client.post(path, json={'confirmed': True, 'expected_revision': 0}).status_code, 403)
            self.assertEqual(self.client.post(path, json={'confirmed': True, 'expected_revision': 0},
                                              headers={'X-CSRF-Token': csrf}).status_code, 200)
            clear.assert_called_once_with(project_id, 0)
        with patch.object(self.app.state.service.sessions, 'browser_ready', return_value={'released': True}) as ready:
            response = self.client.post(f'/api/projects/{project_id}/session/browser-ready', headers={'X-CSRF-Token': csrf})
            self.assertEqual(response.status_code, 200, response.text)
            ready.assert_called_once_with(project_id)
        with patch.object(self.app.state.service, "skip_failed_videos", return_value={"ok": True}) as skip:
            response = self.client.post(f"/api/projects/{project_id}/session/skip-failed", json={"confirmed": True, "expected_revision": 0, "video_ids": ["v0001"]}, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200, response.text)
            skip.assert_called_once_with(project_id, ["v0001"], 0)
        with patch.object(self.app.state.service, "restore_skipped_videos", return_value={"ok": True}) as restore:
            response = self.client.post(f"/api/projects/{project_id}/session/restore-skipped", json={"confirmed": True, "expected_revision": 0}, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200, response.text)
            restore.assert_called_once_with(project_id, 0)
        self.auth.set_active(self.annotator["id"], False)
        self.assertEqual(self.client.get("/api/projects").status_code, 401)

    def test_cloud_sync_retains_final_flow_without_direct_export_endpoint(self):
        project_id = self.project["id"]
        path = f"/api/cloud/projects/{project_id}/sync"
        body = {"final": True}
        self.assertEqual(self.client.post(path, json=body).status_code, 401)
        headers = {"X-CSRF-Token": self.login("worker", "another long safe password")}
        self.assertEqual(self.client.post(path, json=body, headers=headers).status_code, 403)
        self.auth.assign(project_id, self.annotator["id"])
        self.assertEqual(self.client.post(path, json=body).status_code, 403)
        result = {"id": project_id, "revision": 0, "has_documents": True, "nas_relative_path": "fixture/timeline"}
        with patch.object(self.app.state.cloud_mirror, "sync", return_value=result) as sync:
            removed = self.client.post(f"/api/cloud/projects/{project_id}/export", json={"expected_revision": 0}, headers=headers)
            self.assertEqual(removed.status_code, 404, removed.text)
            sync.assert_not_called()
            response = self.client.post(path, json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), result)
            self.assertEqual(sync.call_args.args[0], self.annotator["id"])
            self.assertEqual(sync.call_args.args[1]["id"], project_id)
            self.assertEqual(sync.call_args.kwargs, {"final": True})

    def test_server_owns_attribution_and_records_delete(self):
        project_id = self.project["id"]
        self.auth.assign(project_id, self.annotator["id"])
        csrf = self.login("worker", "another long safe password")
        annotations = empty_annotations()
        annotations["habit"] = [{"id": "habit-1", "label": "喝水", "kind": "point", "start_ms": 100, "end_ms": 100,
                                 "created_by": "forged", "created_by_name": "伪造姓名"}]
        url = f"/api/projects/{project_id}/draft"
        body = {"annotations": annotations, "expected_revision": 0}
        self.assertEqual(self.client.put(url, json=body).status_code, 403)
        self.assertEqual(self.client.put(url, json=body, headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1:9999"}).status_code, 403)
        saved = self.client.put(url, json=body, headers={"X-CSRF-Token": csrf})
        self.assertEqual(saved.status_code, 200, saved.text)
        item = saved.json()["annotations"]["habit"][0]
        self.assertEqual(item["created_by"], self.annotator["id"])
        self.assertEqual(item["created_by_name"], "张标注")
        self.assertNotEqual(item["created_by"], "forged")
        annotations["habit"] = [{**item, "label": "喝热水", "created_by": "forged", "updated_by_name": "伪造编辑者"}]
        updated = self.client.put(url, json={"annotations": annotations, "expected_revision": 1}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(updated.status_code, 200, updated.text)
        changed = updated.json()["annotations"]["habit"][0]
        self.assertEqual(changed["created_by"], self.annotator["id"])
        self.assertEqual(changed["updated_by"], self.annotator["id"])
        self.assertEqual(changed["updated_by_name"], "张标注")
        annotations["habit"] = []
        deleted = self.client.put(url, json={"annotations": annotations, "expected_revision": 2}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        history = self.client.get(f"/api/projects/{project_id}/history").json()
        self.assertEqual([event["action"] for event in history], ["delete", "update", "create"])
        self.assertEqual(history[0]["actor_id"], self.annotator["id"])
        self.assertEqual(history[0]["before"]["created_by"], self.annotator["id"])

    def test_project_creation_route_is_retired_with_csrf_guard(self):
        project_id = self.project['id']
        self.auth.assign(project_id, self.annotator['id'])
        csrf = self.login('worker', 'another long safe password')
        path = f'/api/projects/{project_id}/custom-tracks'
        body = {'name': '环境', 'mode': 'state', 'labels': ['安静'], 'expected_revision': 0}
        self.assertEqual(self.client.post(path, json=body).status_code, 403)
        self.assertEqual(self.client.post(path, json=body, headers={'X-CSRF-Token': csrf}).status_code, 410)

    def test_track_templates_are_private_to_account_and_require_csrf(self):
        self.assertEqual(self.client.get("/api/account/track-templates").status_code, 401)
        csrf = self.login("worker", "another long safe password")
        path = "/api/account/track-templates"
        body = {"name": "饮食", "json_name": "food", "labels": ["进食", "未进食"]}
        self.assertEqual(self.client.put(path, json=body).status_code, 403)
        created = self.client.put(path, json=body, headers={"X-CSRF-Token": csrf})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(self.client.get(path).json(), [body])
        for remaining in (["未进食"], []):
            updated = self.client.put(path, json={**body, "labels": remaining}, headers={"X-CSRF-Token": csrf})
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(self.client.get(path).json()[0]["labels"], remaining)
        invalid = {**body, "labels": ["进食\n伪造"]}
        self.assertEqual(self.client.put(path, json=invalid, headers={"X-CSRF-Token": csrf}).status_code, 422)
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        self.login("admin", "correct horse battery staple")
        self.assertEqual(self.client.get(path).json(), [])

    def test_local_account_deletes_only_its_templates_without_modifying_projects(self):
        path = "/api/account/track-templates"
        body = {"name": "饮食", "json_name": "food", "labels": ["进食"]}
        self.project["custom_tracks"] = [{"id": "food", "name": "饮食", "mode": "state", "labels": ["进食"]}]
        self.project["annotations"]["food"] = [{"id": "food-1", "label": "进食", "start_ms": 0, "end_ms": 1000}]
        self.app.state.service.save(self.project)
        worker_csrf = self.login("worker", "another long safe password")
        self.assertEqual(self.client.put(path, json=body, headers={"X-CSRF-Token": worker_csrf}).status_code, 200)
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": worker_csrf})
        admin_csrf = self.login("admin", "correct horse battery staple")
        self.assertEqual(self.client.put(path, json=body, headers={"X-CSRF-Token": admin_csrf}).status_code, 200)
        self.assertEqual(self.client.delete(path + "/food").status_code, 403)
        self.assertEqual(self.client.delete(path + "/food", headers={"X-CSRF-Token": admin_csrf}).status_code, 200)
        self.assertEqual(self.client.get(path).json(), [])
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": admin_csrf})
        worker_csrf = self.login("worker", "another long safe password")
        self.assertEqual(self.client.get(path).json(), [body])
        self.assertEqual(self.client.delete(path + "/food", headers={"X-CSRF-Token": worker_csrf}).status_code, 200)
        self.assertEqual(self.client.get(path).json(), [])
        stored = self.app.state.service.load(self.project["id"])
        self.assertEqual(stored["annotations"], self.project["annotations"])
        self.assertEqual(stored["revision"], self.project["revision"])
        self.assertEqual(stored["custom_tracks"], self.project["custom_tracks"])

    def test_fixed_axis_entries_are_account_private_persistent_and_recoverable(self):
        path = "/api/account/fixed-track-templates"
        axes = ["scene", "posture", "category", "habit"]
        self.assertEqual(self.client.get(path).status_code, 401)
        csrf = self.login("worker", "another long safe password")
        self.assertEqual(self.client.get(path).json(), axes)
        self.assertEqual(self.client.delete(path + "/category").status_code, 403)
        self.assertEqual(self.client.put(path + "/category").status_code, 403)
        before = self.app.state.service.load(self.project["id"])
        for axis in axes:
            response = self.client.delete(path + "/" + axis, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(axis, response.json())
        self.assertEqual(self.client.get(path).json(), [])
        self.assertEqual(AuthStore(self.root).visible_fixed_track_templates(self.annotator["id"]), [])
        self.assertEqual(self.app.state.service.load(self.project["id"]), before)
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        admin_csrf = self.login("admin", "correct horse battery staple")
        self.assertEqual(self.client.get(path).json(), axes)
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": admin_csrf})
        csrf = self.login("worker", "another long safe password")
        self.assertEqual(self.client.get(path).json(), [])
        for axis in axes:
            response = self.client.put(path + "/" + axis, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(axis, response.json())
        self.assertEqual(self.client.get(path).json(), axes)
        self.assertEqual(self.app.state.service.load(self.project["id"]), before)
        for method in (self.client.delete, self.client.put):
            invalid = method(path + "/food", headers={"X-CSRF-Token": csrf})
            self.assertEqual(invalid.status_code, 404)
        self.assertEqual(self.client.get(path).json(), axes)

    def test_existing_account_database_gains_fixed_axis_preferences_without_losing_templates(self):
        self.auth.save_track_template(self.annotator["id"], "food", "饮食", ["进食"])
        with self.auth.connection() as con:
            con.execute("DROP TABLE hidden_fixed_track_templates")
        migrated = AuthStore(self.root)
        self.assertEqual(migrated.visible_fixed_track_templates(self.annotator["id"]), ["scene", "posture", "category", "habit"])
        self.assertEqual(migrated.list_track_templates(self.annotator["id"]), [{"json_name": "food", "name": "饮食", "labels": ["进食"]}])
        migrated.set_fixed_track_template_visible(self.annotator["id"], "habit", False)
        self.assertNotIn("habit", AuthStore(self.root).visible_fixed_track_templates(self.annotator["id"]))

    def test_cache_collection_setting_preserves_project_permissions_and_csrf(self):
        csrf = self.login("worker", "another long safe password")
        path = f"/api/projects/{self.project['id']}/cache-collection"
        body = {"name": "1010zengl", "expected_revision": 0}
        self.assertEqual(self.client.put(path, json=body, headers={"X-CSRF-Token": csrf}).status_code, 403)
        self.auth.assign(self.project['id'], self.annotator['id'])
        self.assertEqual(self.client.put(path, json=body).status_code, 403)
        invalid = self.client.put(path, json={**body, "name": "../escape"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(invalid.status_code, 422)
        changed = self.client.put(path, json=body, headers={"X-CSRF-Token": csrf})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()['collection_folder_name'], '1010zengl')
        self.assertEqual(changed.json()['annotations'], self.project['annotations'])
        self.assertEqual(self.client.put(path, json=body, headers={"X-CSRF-Token": csrf}).status_code, 409)
        self.assertEqual(self.client.put('/api/cache-location', json={'root': '/tmp/unexpected'}, headers={"X-CSRF-Token": csrf}).status_code, 403)

    def test_admin_controls_accounts_and_assignments(self):
        csrf = self.login("admin", "correct horse battery staple")
        users = self.client.get("/api/users")
        self.assertEqual(users.status_code, 200)
        self.assertTrue(next(user for user in users.json() if user["id"] == self.admin["id"])["can_upload"])
        self.assertFalse(next(user for user in users.json() if user["id"] == self.annotator["id"])["can_upload"])
        permission = f"/api/users/{self.annotator['id']}/upload-permission"
        self.assertEqual(self.client.put(permission, json={"can_upload": True}).status_code, 403)
        self.assertEqual(self.client.put(permission, json={"can_upload": True}, headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertTrue(next(user for user in self.client.get("/api/users").json() if user["id"] == self.annotator["id"])["can_upload"])
        self.assertEqual(self.client.put(permission, json={"can_upload": False}, headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertFalse(next(user for user in self.client.get("/api/users").json() if user["id"] == self.annotator["id"])["can_upload"])
        self.assertEqual(self.client.put(f"/api/users/{self.admin['id']}/upload-permission", json={"can_upload": False}, headers={"X-CSRF-Token": csrf}).status_code, 422)
        project_id = self.project["id"]
        self.assertEqual(self.client.put(f"/api/projects/{project_id}/assignment", json={"user_id": self.annotator["id"]}).status_code, 403)
        assigned = self.client.put(f"/api/projects/{project_id}/assignment", json={"user_id": self.annotator["id"]}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(self.auth.assignment(project_id), self.annotator["id"])

    def test_existing_account_database_gains_disabled_upload_permission(self):
        with tempfile.TemporaryDirectory(prefix="auth-migration-") as root:
            local = Path(root) / ".local"
            local.mkdir()
            with sqlite3.connect(local / "auth.sqlite3") as con:
                con.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE, display_name TEXT, role TEXT, password_hash TEXT, active INTEGER)")
                con.execute("INSERT INTO users VALUES ('old','oldworker','旧账号','annotator','unused',1)")
            con.close()
            migrated = AuthStore(Path(root))
            self.assertFalse(migrated.list_users()[0]["can_upload"])
            self.assertIsNone(migrated.list_users()[0]["email"])
            migrated.set_upload_permission("old", True)
            self.assertTrue(migrated.can_upload("old"))

    def test_account_email_is_optional_unique_and_visible_to_admin(self):
        csrf = self.login("admin", "correct horse battery staple")
        response = self.client.post("/api/users", json={"username": "emailed", "display_name": "测试标注",
            "email": "Worker@Example.com", "password": "another long safe password"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["email"], "worker@example.com")
        self.assertEqual(next(user for user in self.client.get("/api/users").json()
                              if user["username"] == "emailed")["email"], "worker@example.com")
        duplicate = self.client.post("/api/users", json={"username": "duplicate", "display_name": "另一位",
            "email": "worker@example.com", "password": "another long safe password"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(duplicate.status_code, 409)
        self.login("WORKER@example.com", "another long safe password")
        self.assertEqual(self.client.get("/api/auth/me").json()["username"], "emailed")

    def test_administrator_can_create_account_with_numeric_password(self):
        csrf = self.login("admin", "correct horse battery staple")
        created = self.client.post("/api/users", json={"username": "numeric", "display_name": "数字密码账号",
            "password": "12345678"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(created.status_code, 200, created.text)
        self.login("numeric", "12345678")
        csrf = self.login("admin", "correct horse battery staple")
        too_short = self.client.post("/api/users", json={"username": "shortnum", "display_name": "短密码账号",
            "password": "1234567"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(too_short.status_code, 422)
        mixed = self.client.post("/api/users", json={"username": "shortmix", "display_name": "混合密码账号",
            "password": "abc12345"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(mixed.status_code, 422)
        reset = self.client.put(f"/api/users/{created.json()['id']}/password", json={"password": "87654321"},
                                headers={"X-CSRF-Token": csrf})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "numeric",
            "password": "87654321"}).status_code, 200)

    def test_editing_presence_names_other_user_and_expires(self):
        other = self.auth.create_user("second", "第二位", "correct horse battery staple")
        project_id = self.project["id"]
        self.assertTrue(self.auth.claim(project_id, self.annotator["id"]))
        self.assertFalse(self.auth.claim(project_id, other["id"]))
        self.assertEqual(self.auth.enter_editing(project_id, self.annotator["id"], "a" * 36), [])
        others = self.auth.enter_editing(project_id, self.admin["id"], "b" * 36)
        self.assertEqual([item["display_name"] for item in others], ["张标注"])
        self.auth.leave_editing(project_id, self.annotator["id"], "a" * 36)
        self.assertEqual(self.auth.enter_editing(project_id, self.admin["id"], "b" * 36), [])

    def test_destructive_project_track_routes_are_retired(self):
        project_id = self.project['id']
        self.auth.assign(project_id, self.annotator['id'])
        csrf = self.login('worker', 'another long safe password')
        for suffix, body in [('fixed-tracks/posture', {'enabled': False, 'expected_revision': 0}),
                             ('tracks/habit/labels', {'labels': [], 'expected_revision': 0})]:
            path = f'/api/projects/{project_id}/{suffix}'
            self.assertEqual(self.client.put(path, json=body).status_code, 403)
            self.assertEqual(self.client.put(path, json=body, headers={'X-CSRF-Token': csrf}).status_code, 410)

    def test_password_change_revokes_session_and_lockout_persists(self):
        csrf = self.login("worker", "another long safe password")
        changed = self.client.post("/api/auth/password", json={"current_password": "another long safe password", "password": "replacement safe password"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "worker", "password": "another long safe password"}).status_code, 401)
        self.login("worker", "replacement safe password")
        for _ in range(4):
            self.assertEqual(self.client.post("/api/auth/login", json={"username": "worker", "password": "wrong password"}).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "worker", "password": "wrong password"}).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "worker", "password": "replacement safe password"}).status_code, 429)

    def test_internal_https_origin_allows_login_without_exposing_insecure_cookie(self):
        with tempfile.TemporaryDirectory(prefix="intranet-auth-") as folder:
            root = Path(folder)
            AuthStore(root).create_user("admin", "管理员", "correct horse battery staple", "admin")
            with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}):
                app = create_app(root)
            with TestClient(app, base_url="http://10.20.30.40") as client:
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertNotIn("native-file-picker", health.json()["capabilities"])
                rejected = client.post("/api/auth/login", headers={"Origin": "https://other.internal"},
                                       json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(rejected.status_code, 403)
                response = client.post("/api/auth/login", headers={"Origin": "https://10.20.30.40"},
                                       json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(response.status_code, 200, response.text)
                with patch("backend.app.choose_local_paths") as picker:
                    cookie = "; ".join(f"{name}={response.cookies[name]}" for name in ("datamark_session", "datamark_csrf"))
                    selected = client.post("/api/local-files/pick", headers={"Origin": "https://10.20.30.40", "x-csrf-token": response.json()["csrf"], "Cookie": cookie},
                                           json={"kind": "files"})
                    self.assertEqual(selected.status_code, 503, selected.text)
                    picker.assert_not_called()
                self.assertIn("secure", response.headers["set-cookie"].lower())
                with client.websocket_connect("ws://10.20.30.40/api/browser/connection",
                                              headers={"origin": "https://10.20.30.40"}) as socket:
                    socket.send_text("connected")
                    self.assertEqual(len(app.state.browser_lifetime.clients), 1)
            with TestClient(app, base_url="http://wrong.internal") as client:
                self.assertEqual(client.get("/api/health").status_code, 400)

    def test_internal_origin_requires_https_and_existing_admin(self):
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "http://10.20.30.40"}):
            with self.assertRaises(ValueError):
                create_app(self.root)
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40", "DATAMARK_BIND_IP": "203.0.113.9"}):
            with self.assertRaisesRegex(ValueError, "RFC1918"):
                create_app(self.root)
        with tempfile.TemporaryDirectory(prefix="empty-intranet-") as folder:
            with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40"}):
                app = create_app(Path(folder))
            with self.assertRaisesRegex(RuntimeError, "管理员"):
                with TestClient(app, base_url="http://10.20.30.40"):
                    pass

    def test_internal_https_custom_port_uses_exact_origin(self):
        with tempfile.TemporaryDirectory(prefix="intranet-port-") as folder:
            root = Path(folder)
            AuthStore(root).create_user("admin", "管理员", "correct horse battery staple", "admin")
            with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40:10443",
                                            "DATAMARK_STORAGE_BACKEND": "nas"}):
                app = create_app(root)
            with TestClient(app, base_url="https://10.20.30.40:10443") as client:
                denied = client.post("/api/auth/login", headers={"Origin": "https://10.20.30.40"},
                                     json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(denied.status_code, 403)
                login = client.post("/api/auth/login", headers={"Origin": "https://10.20.30.40:10443"},
                                    json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(login.status_code, 200, login.text)
                self.assertIn("secure", login.headers["set-cookie"].lower())
                self.assertEqual(client.get("/api/health").json()["storage_backend"], "nas")

    def test_nas_deployment_rejects_public_api_origin(self):
        with tempfile.TemporaryDirectory(prefix="public-auth-") as folder:
            root = Path(folder)
            AuthStore(root).create_user("admin", "管理员", "correct horse battery staple", "admin")
            with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40",
                                            "DATAMARK_STORAGE_BACKEND": "nas",
                                            "DATAMARK_PUBLIC_ORIGIN": "https://annotate.example.com",
                                            "DATAMARK_PUBLIC_API_ORIGIN": "https://api-annotate.example.com:10443"}):
                with self.assertRaisesRegex(ValueError, "isolated S3 deployment"):
                    create_app(root)
            with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40",
                                            "DATAMARK_STORAGE_BACKEND": "nas",
                                            "DATAMARK_PUBLIC_ORIGIN": "",
                                            "DATAMARK_PUBLIC_API_ORIGIN": ""}):
                app = create_app(root)
            with TestClient(app, base_url="https://10.20.30.40") as intranet:
                self.assertEqual(intranet.get("/api/health").json()["storage_backend"], "nas")
            with TestClient(app, base_url="https://api-annotate.example.com") as public:
                self.assertEqual(public.get("/api/health").status_code, 400)

    def test_public_origin_must_be_an_exact_https_origin(self):
        for public_origin in ("http://annotate.example.com", "https://annotate.example.com:443",
                              "https://annotate.example.com/path",
                              "https://*.example.com", "https://annotate.example.com:invalid",
                              "https://bad..example.com"):
            with self.subTest(public_origin=public_origin):
                with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40",
                                                "DATAMARK_PUBLIC_ORIGIN": public_origin,
                                                "DATAMARK_PUBLIC_API_ORIGIN": "https://api-annotate.example.com"}):
                    with self.assertRaises(ValueError):
                        create_app(self.root)
        with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40",
                                        "DATAMARK_PUBLIC_ORIGIN": "https://annotate.example.com"}):
            with self.assertRaises(ValueError):
                create_app(self.root)
        for api_origin in ("http://api.example.com", "https://api.example.com:443",
                           "https://api.example.com/path", "https://bad..example.com",
                           "https://annotate.example.com"):
            with self.subTest(api_origin=api_origin):
                with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40",
                                                "DATAMARK_PUBLIC_ORIGIN": "https://annotate.example.com",
                                                "DATAMARK_PUBLIC_API_ORIGIN": api_origin}):
                    with self.assertRaises(ValueError):
                        create_app(self.root)


if __name__ == "__main__":
    unittest.main()
