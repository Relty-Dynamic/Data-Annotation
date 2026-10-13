"""Desktop launcher behavior when startup is slow or clicked repeatedly."""
import threading
import json
import os
from pathlib import Path
import tempfile
import urllib.request
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import launch


class LauncherTests(unittest.TestCase):
    def test_restart_rejects_an_unrelated_application(self):
        with patch.object(launch, "health", return_value={"application": "other"}), patch.object(launch.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "其他应用"):
                launch.restart_running()
            run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows desktop restart")
    def test_process_ownership_requires_the_exact_script_argument(self):
        script = str(launch.ROOT / "launch.py")
        for command, owned in ((f'python.exe "{script}" --no-open', True),
                               (f'python.exe "{script}.other"', False),
                               ('python.exe other-project/launch.py', False)):
            with self.subTest(command=command), patch.object(launch.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps({"Name": "python.exe", "CommandLine": command}))):
                self.assertEqual(launch.owned_windows_process(123), owned)

    @unittest.skipUnless(os.name == "nt", "Windows desktop restart")
    def test_restart_refuses_stale_pid_without_signalling_or_killing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".local").mkdir()
            (root / ".local/server.pid").write_text("123", encoding="ascii")
            with patch.object(launch, "ROOT", root), patch.object(launch, "health", return_value={"application": "datamark"}), patch.object(launch, "owned_windows_process", return_value=False), patch.object(launch.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "不符"):
                    launch.restart_running()
                run.assert_not_called()
                self.assertFalse((root / ".local/server.stop").exists())

    @unittest.skipUnless(os.name == "nt", "Windows desktop restart")
    def test_restart_signals_owned_server_and_waits_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".local").mkdir()
            (root / ".local/server.pid").write_text("123", encoding="ascii")
            observations = [{"application": "datamark"}, {"application": "datamark"}, None, None]
            def wait_for_stop(_):
                self.assertEqual((root / ".local/server.stop").read_text(encoding="ascii"), "123")
            with patch.object(launch, "ROOT", root), patch.object(launch, "health", side_effect=observations), patch.object(launch, "owned_windows_process", return_value=True), patch.object(launch.time, "sleep", side_effect=wait_for_stop), patch.object(launch.subprocess, "run") as run:
                launch.restart_running()
                run.assert_not_called()
                self.assertFalse((root / ".local/server.stop").exists())

    def test_reuses_running_server_and_opens_browser(self):
        args = SimpleNamespace(no_browser=False, no_open=False)
        with patch.object(launch, "health", return_value={"application": "datamark", "stopping": False, "browser_lifetime": True, "capabilities": ["account-login", "native-file-picker"]}), patch.object(launch, "reserve_browser", return_value=True) as reserve, patch.object(launch, "open_browser") as open_browser:
            self.assertTrue(launch.reuse_running(args))
            reserve.assert_called_once_with()
            open_browser.assert_called_once_with()

    def test_stopping_server_is_not_reused(self):
        args = SimpleNamespace(no_browser=False, no_open=False)
        with patch.object(launch, "health", return_value={"application": "datamark", "stopping": True}), patch.object(launch, "open_browser") as open_browser:
            self.assertFalse(launch.reuse_running(args))
            open_browser.assert_not_called()

    def test_old_server_without_login_is_not_reused(self):
        args = SimpleNamespace(no_browser=False, no_open=False)
        with patch.object(launch, "health", return_value={"application": "datamark", "stopping": False, "browser_lifetime": True, "capabilities": ["four-axis-annotations"]}), patch.object(launch, "open_browser") as open_browser:
            self.assertFalse(launch.reuse_running(args))
            open_browser.assert_not_called()

    def test_browser_waits_until_server_is_actually_ready(self):
        server = SimpleNamespace(started=False)
        stop = threading.Event()
        with patch.object(launch, "open_browser") as open_browser:
            thread = threading.Thread(target=launch.open_when_ready, args=(server, stop), daemon=True)
            thread.start()
            self.assertFalse(open_browser.called)
            server.started = True
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            open_browser.assert_called_once_with()

    def test_failed_port_claim_closes_socket(self):
        from unittest.mock import MagicMock
        listener = MagicMock()
        listener.bind.side_effect = OSError("port busy")
        with patch.object(launch.socket, "socket", return_value=listener):
            with self.assertRaises(OSError):
                launch.bind_listener()
        listener.close.assert_called_once_with()


class LauncherSocketIntegrationTests(unittest.TestCase):
    def test_claimed_socket_serves_and_rejects_second_launcher(self):
        import uvicorn
        listener = launch.bind_listener(port=0)
        port = listener.getsockname()[1]
        with self.assertRaises(OSError):
            second = launch.socket.socket()
            try:
                second.bind(("127.0.0.1", port))
            finally:
                second.close()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ready"})

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                threading.Event().wait(.05)
            self.assertTrue(server.started)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                self.assertEqual(response.read(), b"ready")
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            listener.close()
        self.assertFalse(thread.is_alive())
