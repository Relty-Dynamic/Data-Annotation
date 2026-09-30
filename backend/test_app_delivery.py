"""The application shell must update without discarding reusable hashed assets."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from backend.app import create_app

ROOT = Path(__file__).resolve().parents[1]


class AppDeliveryTests(unittest.TestCase):
    def test_importing_app_factory_does_not_open_a_real_project_database(self):
        with tempfile.TemporaryDirectory(prefix="delivery-import-test-", dir=ROOT / ".tmp") as folder:
            root = Path(folder)
            env = {**os.environ, "DATAMARK_ROOT": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-B", "-c", "import backend.app"],
                                    cwd=ROOT, env=env, capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / ".local").exists())

    def test_reopening_revalidates_html_and_picks_up_a_changed_bundle(self):
        with tempfile.TemporaryDirectory(prefix="delivery-test-", dir=ROOT / ".tmp") as folder:
            root = Path(folder)
            dist = root / "frontend" / "dist"
            assets = dist / "assets"
            assets.mkdir(parents=True)
            index = dist / "index.html"
            index.write_text('<script src="/assets/index-abcdefgh.js"></script>', encoding="utf-8")
            (assets / "index-abcdefgh.js").write_text("old bundle", encoding="utf-8")
            (assets / "helper.js").write_text("not fingerprinted", encoding="utf-8")
            app = create_app(root, auth_required=False)
            with TestClient(app, base_url="http://127.0.0.1") as client:
                for route in ("/", "/index.html"):
                    old = client.get(route)
                    self.assertEqual(old.status_code, 200)
                    self.assertEqual(old.headers["cache-control"], "no-cache")
                    unchanged = client.get(route, headers={"If-None-Match": old.headers["etag"]})
                    self.assertEqual(unchanged.status_code, 304)
                    self.assertEqual(unchanged.headers["cache-control"], "no-cache")
                index.write_text('<script src="/assets/index-ijklmnop.js"></script><!-- updated -->', encoding="utf-8")
                changed = client.get("/", headers={"If-None-Match": old.headers["etag"]})
                self.assertEqual(changed.status_code, 200)
                self.assertIn("index-ijklmnop.js", changed.text)
                hashed = client.get("/assets/index-abcdefgh.js")
                self.assertIn("immutable", hashed.headers["cache-control"])
                self.assertNotIn("immutable", client.get("/assets/helper.js").headers.get("cache-control", ""))
                self.assertNotIn("immutable", client.get("/assets/missing-abcdefgh.js").headers.get("cache-control", ""))
                self.assertEqual(client.get("/api/projects").headers["cache-control"], "no-store")
