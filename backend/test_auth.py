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
        self.assertEqual(self.client.get(f"/api/%70rojects/{project_id}").status_code, 403)
        self.assertEqual(self.client.get(f"/api/session-media/{project_id}/v1/v0001").status_code, 403)
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/writeback", headers={"X-CSRF-Token": csrf}).status_code, 403)
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.auth.assign(project_id, self.annotator["id"])
        self.assertEqual(len(self.client.get("/api/projects").json()), 1)
        self.assertEqual(self.client.get(f"/api/projects/{project_id}").status_code, 200)
        with patch.object(self.app.state.service, "writeback", return_value={"ok": True}) as writeback:
            self.assertEqual(self.client.post(f"/api/projects/{project_id}/writeback", headers={"X-CSRF-Token": csrf}).status_code, 200)
            writeback.assert_called_once_with(project_id)
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

    def test_assigned_annotator_can_add_custom_axis_and_save_it(self):
        project_id = self.project["id"]
        self.auth.assign(project_id, self.annotator["id"])
        csrf = self.login("worker", "another long safe password")
        path = f"/api/projects/{project_id}/custom-tracks"
        body = {"name": "环境", "mode": "state", "labels": ["安静", "嘈杂"], "expected_revision": 0}
        self.assertEqual(self.client.post(path, json=body).status_code, 403)
        created = self.client.post(path, json=body, headers={"X-CSRF-Token": csrf})
        self.assertEqual(created.status_code, 200, created.text)
        axis = created.json()["custom_tracks"][0]["id"]
        annotations = created.json()["annotations"]
        annotations[axis] = [{"id": "quiet", "label": "安静", "kind": "interval", "start_ms": 0, "end_ms": 1000}]
        saved = self.client.put(f"/api/projects/{project_id}/draft",
                                json={"annotations": annotations, "expected_revision": 1},
                                headers={"X-CSRF-Token": csrf})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["annotations"][axis][0]["created_by"], self.annotator["id"])
        removed = self.client.request("DELETE", f"{path}/{axis}",
                                      json={"confirmed": True, "expected_revision": saved.json()["revision"]},
                                      headers={"X-CSRF-Token": csrf})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertNotIn(axis, removed.json()["annotations"])

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

    def test_assigned_annotator_can_remove_and_restore_fixed_track_with_revision_guard(self):
        project_id = self.project["id"]
        self.auth.assign(project_id, self.annotator["id"])
        csrf = self.login("worker", "another long safe password")
        path = f"/api/projects/{project_id}/fixed-tracks/posture"
        body = {"enabled": False, "expected_revision": 0}
        self.assertEqual(self.client.put(path, json=body).status_code, 403)
        removed = self.client.put(path, json=body, headers={"X-CSRF-Token": csrf})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertNotIn("posture", removed.json()["fixed_tracks"])
        self.assertEqual(removed.json()["annotations"]["posture"], [])
        self.assertEqual(self.client.put(path, json=body, headers={"X-CSRF-Token": csrf}).status_code, 409)
        restored = self.client.put(path, json={"enabled": True, "expected_revision": removed.json()["revision"]},
                                   headers={"X-CSRF-Token": csrf})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertIn("posture", restored.json()["fixed_tracks"])

    def test_assigned_annotator_manages_labels_with_csrf_and_delete_history(self):
        project_id = self.project["id"]
        self.auth.assign(project_id, self.annotator["id"])
        csrf = self.login("worker", "another long safe password")
        path = f"/api/projects/{project_id}/tracks/habit/labels"
        body = {"labels": ["喝水"], "expected_revision": 0}
        self.assertEqual(self.client.put(path, json=body).status_code, 403)
        added = self.client.put(path, json=body, headers={"X-CSRF-Token": csrf})
        self.assertEqual(added.status_code, 200, added.text)
        annotations = added.json()["annotations"]
        annotations["habit"] = [{"id": "one", "label": "喝水", "kind": "point", "start_ms": 100, "end_ms": 100}]
        saved = self.client.put(f"/api/projects/{project_id}/draft", json={"annotations": annotations, "expected_revision": added.json()["revision"]}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(saved.status_code, 200, saved.text)
        removed = self.client.put(path, json={"labels": [], "expected_revision": saved.json()["revision"]}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json()["annotations"]["habit"], [])
        self.assertEqual(self.client.put(path, json={"labels": [], "expected_revision": saved.json()["revision"]}, headers={"X-CSRF-Token": csrf}).status_code, 409)
        history = self.client.get(f"/api/projects/{project_id}/history").json()
        self.assertEqual(history[0]["action"], "delete")
        self.assertEqual(history[0]["actor_id"], self.annotator["id"])

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

    def test_public_https_origin_keeps_intranet_origin_and_rejects_other_hosts(self):
        with tempfile.TemporaryDirectory(prefix="public-auth-") as folder:
            root = Path(folder)
            AuthStore(root).create_user("admin", "管理员", "correct horse battery staple", "admin")
            with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40",
                                            "DATAMARK_PUBLIC_ORIGIN": "https://annotate.example.com",
                                            "DATAMARK_PUBLIC_API_ORIGIN": "https://api-annotate.example.com:10443"}):
                app = create_app(root)
            with TestClient(app, base_url="https://api-annotate.example.com:10443") as public:
                self.assertEqual(public.get("/api/health").status_code, 200)
                preflight = public.options("/api/auth/login", headers={
                    "Origin": "https://annotate.example.com", "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type"})
                self.assertEqual(preflight.status_code, 200)
                self.assertEqual(preflight.headers["access-control-allow-origin"], "https://annotate.example.com")
                self.assertEqual(preflight.headers["access-control-allow-credentials"], "true")
                media_preflight = public.options("/api/session-media/test-project/v1/v0001", headers={
                    "Origin": "https://annotate.example.com", "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "range"})
                self.assertEqual(media_preflight.status_code, 200)
                rejected = public.post("/api/auth/login", headers={"Origin": "https://other.example.com"},
                                       json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(rejected.status_code, 403)
                response = public.post("/api/auth/login", headers={"Origin": "https://annotate.example.com"},
                                       json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["access-control-allow-origin"], "https://annotate.example.com")
                self.assertIn("secure", response.headers["set-cookie"].lower())
                me = public.get("/api/auth/me", headers={"Origin": "https://annotate.example.com"})
                self.assertEqual(me.status_code, 200)
                self.assertEqual(me.json()["csrf"], response.json()["csrf"])
                media_file = root / "public-range-test.mp4"
                media_file.write_bytes(b"0123456789")
                with patch.object(app.state.service.sessions, "asset", return_value=media_file):
                    media = public.get("/api/session-media/test-project/v1/v0001", headers={
                        "Origin": "https://annotate.example.com", "Range": "bytes=2-5"})
                self.assertEqual((media.status_code, media.content), (206, b"2345"))
                self.assertEqual(media.headers["content-range"], "bytes 2-5/10")
                self.assertEqual(media.headers["access-control-allow-origin"], "https://annotate.example.com")
                self.assertEqual(public.post("/api/auth/logout", headers={"Origin": "https://annotate.example.com",
                                                                "X-CSRF-Token": me.json()["csrf"]}).status_code, 200)
                with public.websocket_connect("ws://api-annotate.example.com:10443/api/browser/connection",
                                              headers={"origin": "https://annotate.example.com"}) as socket:
                    socket.send_text("connected")
            with TestClient(app, base_url="http://10.20.30.40") as intranet:
                self.assertEqual(intranet.get("/api/health").status_code, 200)
            with TestClient(app, base_url="http://annotate.example.com") as other:
                self.assertEqual(other.get("/api/health").status_code, 400)

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
