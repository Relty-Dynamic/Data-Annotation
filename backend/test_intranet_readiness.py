"""Readiness retries and release rollback use isolated clocks, commands and state."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy.wait_intranet_ready import ReadinessError, wait_ready


ROOT = Path(__file__).resolve().parents[1]
HEALTH = dict(status="ok", application="datamark", storage_backend="nas",
              capabilities=["account-login"])


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.clock = patch("deploy.wait_intranet_ready.time.monotonic", side_effect=lambda: self.now)
        self.sleep = patch("deploy.wait_intranet_ready.time.sleep", side_effect=self.advance)
        self.clock.start()
        self.sleep.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.sleep.stop)

    def advance(self, seconds):
        self.now += seconds

    def response(self, code=0, body=None):
        return subprocess.CompletedProcess([], code, json.dumps(HEALTH) if body is None else body, "")

    def test_listener_refusal_and_startup_http_error_recover(self):
        with patch("deploy.wait_intranet_ready.subprocess.run",
                   side_effect=[self.response(7), self.response(22), self.response()]) as run:
            wait_ready("https://127.0.0.1:10443/api/health", "/test/ca.crt")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(self.now, 4)
        for call in run.call_args_list:
            args = call.args[0]
            self.assertEqual(args[args.index("--cacert") + 1], "/test/ca.crt")
            self.assertNotIn("--insecure", args)
            self.assertEqual(call.kwargs["timeout"], 5)

    def test_persistent_refusal_expires_without_another_attempt(self):
        with patch("deploy.wait_intranet_ready.subprocess.run", return_value=self.response(7)) as run:
            with self.assertRaisesRegex(ReadinessError, "within 5s"):
                wait_ready("https://example.invalid", "/ca", timeout=5)
        self.assertEqual(self.now, 5)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.kwargs["timeout"], 1)

    def test_stalled_request_uses_remaining_budget(self):
        def stalled(*args, **kwargs):
            self.advance(kwargs["timeout"])
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        with patch("deploy.wait_intranet_ready.subprocess.run", side_effect=stalled) as run:
            with self.assertRaisesRegex(ReadinessError, "timed out"):
                wait_ready("https://example.invalid", "/ca", timeout=6)
        self.assertEqual(self.now, 6)
        self.assertEqual(run.call_count, 1)

    def test_certificate_failure_is_immediate(self):
        with patch("deploy.wait_intranet_ready.subprocess.run", return_value=self.response(60)) as run:
            with self.assertRaisesRegex(ReadinessError, "curl exit 60"):
                wait_ready("https://example.invalid", "/ca")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.now, 0)

    def test_wrong_service_and_malformed_responses_never_publish(self):
        bodies = ["", "not JSON", "null", "[]"]
        for key, value in [("status", "starting"), ("application", "other"),
                           ("storage_backend", "s3"), ("capabilities", []),
                           ("capabilities", "account-login")]:
            bodies.append(json.dumps({**HEALTH, key: value}))
        for body in bodies:
            with self.subTest(body=body), patch("deploy.wait_intranet_ready.subprocess.run",
                                               return_value=self.response(body=body)) as run:
                with self.assertRaises(ReadinessError):
                    wait_ready("https://example.invalid", "/ca")
                self.assertEqual(run.call_count, 1)
        self.assertEqual(self.now, 0)


@unittest.skipUnless(shutil.which("bash"), "deployment entrypoint requires bash")
class IntranetReleaseTests(unittest.TestCase):
    """Run the actual shell entrypoint; fake only host commands and network IO."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "service"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.old, self.new = "a" * 40, "b" * 40
        self.previous = self.home / "releases" / self.old
        release = self.home / "releases" / self.new
        for path in (self.previous, release):
            (path / "deploy").mkdir(parents=True)
            (path / ".release-commit").write_text(path.name)
            (path / "deploy/compose.intranet.yaml").write_text("fixture compose")
        (self.home / "current").symlink_to(self.previous)
        state = self.home / "state"
        state.mkdir()
        for name in ("auth.sqlite3", "annotations.sqlite3"):
            with sqlite3.connect(state / name) as connection:
                connection.execute("CREATE TABLE records(value)")
                connection.execute("INSERT INTO records VALUES ('preserve live state')")
        ca = self.home / "caddy-data/caddy/pki/authorities/local/root.crt"
        ca.parent.mkdir(parents=True)
        ca.write_text("fixture CA consumed only by fake curl")
        self.env_file = self.home / "intranet.env"
        self.env_file.write_text(
            f"DATAMARK_HOME={self.home}\nDATAMARK_HOST=127.0.0.1\n"
            "DATAMARK_HTTPS_PORT=10443\nDATAMARK_ORIGIN_PORT_SUFFIX=:10443\n"
            f"DATAMARK_WEB_IMAGE_TAG={self.old}\n")
        self.image = self.root / "image"
        self.image.write_text(self.old)
        self.calls = self.root / "docker-calls"
        self.curl_calls = self.root / "curl-calls"
        self.script = self.root / "deploy.sh"
        self.script.write_text((ROOT / "deploy/ci-deploy-intranet.sh").read_text().replace(
            "/home/relty/services/datamark-web", str(self.home)))
        # Keep the real probe/retry implementation; advance a virtual deadline
        # between attempts so CI process startup speed cannot make this flaky.
        (release / "deploy/wait_intranet_ready.py").write_text(
            f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
            "from deploy import wait_intranet_ready as ready\n"
            "clock = [0]\nready.time.monotonic = lambda: clock[0]\n"
            "ready.time.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)\n"
            "ready.wait_ready(*sys.argv[1:], timeout=15, interval=5)\n")
        self.command("python3", f"import os,sys\nos.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])")
        self.command("id", "print('relty')")
        self.command("flock", "")
        self.command("git", f"import sys\nif sys.argv[1]=='rev-parse': print({self.new!r})")
        self.command("mv", "import os,sys\nos.replace(sys.argv[-2],sys.argv[-1])")
        self.command("docker", f"""
import os,sys
from pathlib import Path
args=sys.argv[1:]
with Path({str(self.calls)!r}).open('a') as log: log.write(' '.join(args)+'\\n')
image=Path({str(self.image)!r})
if args[:2]==['inspect','datamark-intranet-public_gateway-1']: print('false')
elif args[:2]==['inspect','datamark-intranet-web-1']: print('datamark-web:'+image.read_text())
elif args[0]=='compose' and 'up' in args: image.write_text(os.environ['DATAMARK_WEB_IMAGE_TAG'])
""")

    def command(self, name, source):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n{source}\n")
        path.chmod(0o755)

    def execute(self, recover):
        self.command("curl", f"""
import json,sys
from pathlib import Path
path=Path({str(self.curl_calls)!r})
n=int(path.read_text())+1 if path.exists() else 1
path.write_text(str(n))
if n==1 or not {recover!r}: sys.exit(7)
print({json.dumps(HEALTH)!r})
""")
        result = subprocess.run(["bash", str(self.script), "s3-mock-10443"],
                                env={**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]},
                                capture_output=True, text=True, timeout=15)
        self.assertTrue(self.curl_calls.exists(), result.stdout + result.stderr)
        self.assertGreaterEqual(int(self.curl_calls.read_text()), 2, result.stderr)
        self.assertTrue(list((self.home / "backups").glob("*/auth.sqlite3")))
        with sqlite3.connect(self.home / "state/annotations.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT value FROM records").fetchall(),
                             [("preserve live state",)])
        return result

    def test_late_gateway_publishes_only_after_https_is_ready(self):
        result = self.execute(recover=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "current").resolve().name, self.new)
        self.assertEqual(self.image.read_text(), self.new)
        self.assertIn(f"DATAMARK_WEB_IMAGE_TAG={self.new}", self.env_file.read_text())
        self.assertNotIn("restoring prior containers", result.stderr)

    def test_timeout_rolls_back_container_and_keeps_release_pointer_and_config(self):
        original = self.env_file.read_bytes()
        result = self.execute(recover=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring prior containers", result.stderr)
        self.assertEqual((self.home / "current").resolve(), self.previous)
        self.assertEqual(self.image.read_text(), self.old)
        self.assertEqual(self.env_file.read_bytes(), original)
        self.assertEqual(self.calls.read_text().count("up -d"), 2)


if __name__ == "__main__":
    unittest.main()
