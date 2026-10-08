"""Login, project authorization and server-owned annotation history."""
from __future__ import annotations

import tempfile
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

    def test_admin_controls_accounts_and_assignments(self):
        csrf = self.login("admin", "correct horse battery staple")
        users = self.client.get("/api/users")
        self.assertEqual(users.status_code, 200)
        project_id = self.project["id"]
        self.assertEqual(self.client.put(f"/api/projects/{project_id}/assignment", json={"user_id": self.annotator["id"]}).status_code, 403)
        assigned = self.client.put(f"/api/projects/{project_id}/assignment", json={"user_id": self.annotator["id"]}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(self.auth.assignment(project_id), self.annotator["id"])

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
                                            "DATAMARK_PUBLIC_ORIGIN": "https://annotate.example.com"}):
                app = create_app(root)
            with TestClient(app, base_url="http://annotate.example.com") as public:
                self.assertEqual(public.get("/api/health").status_code, 200)
                rejected = public.post("/api/auth/login", headers={"Origin": "https://other.example.com"},
                                       json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(rejected.status_code, 403)
                response = public.post("/api/auth/login", headers={"Origin": "https://annotate.example.com"},
                                       json={"username": "admin", "password": "correct horse battery staple"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("secure", response.headers["set-cookie"].lower())
                with public.websocket_connect("ws://annotate.example.com/api/browser/connection",
                                              headers={"origin": "https://annotate.example.com"}) as socket:
                    socket.send_text("connected")
            with TestClient(app, base_url="http://10.20.30.40") as intranet:
                self.assertEqual(intranet.get("/api/health").status_code, 200)
            with TestClient(app, base_url="http://other.example.com") as other:
                self.assertEqual(other.get("/api/health").status_code, 400)

    def test_public_origin_must_be_an_exact_https_origin(self):
        for public_origin in ("http://annotate.example.com", "https://annotate.example.com/path",
                              "https://*.example.com", "https://annotate.example.com:invalid",
                              "https://bad..example.com"):
            with self.subTest(public_origin=public_origin):
                with patch.dict("os.environ", {"DATAMARK_ORIGIN": "https://10.20.30.40",
                                                "DATAMARK_PUBLIC_ORIGIN": public_origin}):
                    with self.assertRaises(ValueError):
                        create_app(self.root)


if __name__ == "__main__":
    unittest.main()
