from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.native_picker import choose_local_paths

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
