"""Desktop launcher behavior when startup is slow or clicked repeatedly."""
import threading
import urllib.request
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import launch


class LauncherTests(unittest.TestCase):
    def test_reuses_running_server_and_opens_browser(self):
        args = SimpleNamespace(no_browser=False, no_open=False)
        with patch.object(launch, "health", return_value={"application": "datamark", "stopping": False, "browser_lifetime": True}), patch.object(launch, "reserve_browser", return_value=True) as reserve, patch.object(launch, "open_browser") as open_browser:
            self.assertTrue(launch.reuse_running(args))
            reserve.assert_called_once_with()
            open_browser.assert_called_once_with()

    def test_stopping_server_is_not_reused(self):
        args = SimpleNamespace(no_browser=False, no_open=False)
        with patch.object(launch, "health", return_value={"application": "datamark", "stopping": True}), patch.object(launch, "open_browser") as open_browser:
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
