from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.native_picker import choose_local_paths
from launch import reuse_running

ROOT = Path(__file__).resolve().parents[1]


class NativePickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="picker-tests-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.assertEqual(self.root.resolve().parent, (ROOT / ".tmp").resolve())
        self.temp.cleanup()

    def respond(self, paths):
        def run(args, **kwargs):
            Path(args[-1]).write_text(json.dumps({"paths": paths}), encoding="utf-8")
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            return subprocess.CompletedProcess(args, 0)
        return run

    def test_picker_returns_paths_only_and_cleans_temporary_response(self):
        paths = [str(self.root / "原视频" / "A09999_20260917120000_0000.avi")]
        with patch("backend.native_picker.os") as fake_os, patch("subprocess.CREATE_NO_WINDOW", 0, create=True), patch("backend.native_picker.subprocess.run", side_effect=self.respond(paths)):
            fake_os.name = "nt"
            self.assertEqual(choose_local_paths(self.root, "files"), paths)
        self.assertEqual(list((self.root / ".tmp").iterdir()), [])
        self.assertFalse(Path(paths[0]).exists())

    def test_mac_picker_returns_selected_files_and_directory(self):
        cases = {"files": [str(self.root / "原视频" / "first.avi"), str(self.root / "原视频" / "second.mp4")],
                 "directory": [str(self.root / "原视频")]}
        for kind, paths in cases.items():
            with self.subTest(kind=kind), patch("backend.native_picker.os") as fake_os, patch("backend.native_picker.sys.platform", "darwin"), patch("backend.native_picker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"paths": paths}))) as run:
                fake_os.name = "posix"
                self.assertEqual(choose_local_paths(self.root, kind), paths)
                self.assertEqual(run.call_args.args[0][:4], ["/usr/bin/osascript", "-l", "JavaScript", "-e"])
                self.assertEqual(run.call_args.args[0][-1], kind)
                self.assertNotIn("creationflags", run.call_args.kwargs)
                self.assertFalse((self.root / ".tmp").exists())

    def test_mac_picker_cancel_returns_no_paths(self):
        with patch("backend.native_picker.os") as fake_os, patch("backend.native_picker.sys.platform", "darwin"), patch("backend.native_picker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{"paths": []}')):
            fake_os.name = "posix"
            self.assertEqual(choose_local_paths(self.root, "files"), [])

    def test_linux_picker_does_not_start_a_desktop_dialog(self):
        with patch("backend.native_picker.os") as fake_os, patch("backend.native_picker.sys.platform", "linux"), patch("backend.native_picker.subprocess.run") as run:
            fake_os.name = "posix"
            with self.assertRaises(HTTPException) as context:
                choose_local_paths(self.root, "files")
        self.assertEqual(context.exception.status_code, 503)
        run.assert_not_called()

    def test_local_health_advertises_picker_when_platform_supports_it(self):
        app = create_app(self.root, auth_required=False)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            with patch("backend.app.native_picker_available", return_value=True):
                self.assertIn("native-file-picker", client.get("/api/health").json()["capabilities"])
            with patch("backend.app.native_picker_available", return_value=False):
                self.assertNotIn("native-file-picker", client.get("/api/health").json()["capabilities"])

    def test_mac_launcher_does_not_reuse_an_old_service_without_picker(self):
        old_health = {"application": "datamark", "capabilities": ["account-login"]}
        with patch("launch.sys.platform", "darwin"), patch("launch.health", return_value=old_health), patch("launch.open_browser") as browser:
            self.assertFalse(reuse_running(SimpleNamespace(no_browser=False, no_open=False)))
            browser.assert_not_called()

    def test_cancel_returns_empty_without_creating_a_project(self):
        app = create_app(self.root, auth_required=False)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            with patch("backend.app.choose_local_paths", return_value=[]):
                response = client.post("/api/local-files/pick", json={"kind": "directory"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"paths": []})
            self.assertEqual(client.get("/api/projects").json(), [])
            self.assertIn("source-local-cache", client.get("/api/health").json()["capabilities"])

    def test_timeout_has_actionable_error_and_cleans_response(self):
        with patch("backend.native_picker.os") as fake_os, patch("subprocess.CREATE_NO_WINDOW", 0, create=True), patch("backend.native_picker.subprocess.run", side_effect=subprocess.TimeoutExpired("picker", 900)):
            fake_os.name = "nt"
            with self.assertRaises(HTTPException) as context:
                choose_local_paths(self.root, "files")
        self.assertEqual(context.exception.status_code, 408)
        self.assertEqual(list((self.root / ".tmp").iterdir()), [])

    def test_foreign_origin_cannot_open_a_native_dialog(self):
        app = create_app(self.root, auth_required=False)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            with patch("backend.app.choose_local_paths") as picker:
                response = client.post("/api/local-files/pick", json={"kind": "files"}, headers={"Origin": "https://outside.example"})
                self.assertEqual(response.status_code, 403)
                picker.assert_not_called()

    def test_legacy_uploads_are_rejected_without_parsing_or_saving_video_bytes(self):
        app = create_app(self.root, auth_required=False)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file())
            with patch("starlette.requests.Request.form", side_effect=AssertionError("must not parse multipart")):
                for url in ("/api/projects/upload", "/api/projects/" + "a" * 32 + "/videos/upload"):
                    response = client.post(url, files=[("files", ("A09999_20260917120000_0000.avi", b"x" * 100, "video/x-msvideo"))])
                    self.assertEqual(response.status_code, 410, response.text)
            self.assertEqual(client.get("/api/projects").json(), [])
            after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file())
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
