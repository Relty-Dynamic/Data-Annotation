import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from backend.browser_lifetime import BrowserLifetime
from backend.app import create_app

ROOT = Path(__file__).resolve().parents[1]

class BrowserLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.time = 0
        self.stops = []
        self.life = BrowserLifetime(lambda: self.stops.append(True), clock=lambda: self.time)

    def test_no_browser_startup_timeout_and_one_shot_shutdown(self):
        self.time = 119
        self.assertFalse(self.life.check())
        self.time = 120
        self.assertTrue(self.life.check())
        self.assertFalse(self.life.check())
        self.assertEqual(self.stops, [True])
        self.assertFalse(self.life.connect("late"))
        self.assertFalse(self.life.reserve())

    def test_last_tab_only_and_reload_grace(self):
        self.life.connect("first")
        self.life.connect("second")
        self.life.disconnect("first")
        self.time = 500
        self.assertFalse(self.life.check())
        self.life.disconnect("second")
        self.time = 514
        self.assertFalse(self.life.check())
        self.life.connect("reload")
        self.time = 10000  # A hidden page remains alive without JavaScript timers.
        self.assertFalse(self.life.check())
        self.life.disconnect("reload")
        self.time += 15
        self.assertTrue(self.life.check())

    def test_active_save_blocks_shutdown_and_launcher_reserves_new_page(self):
        self.life.connect("page")
        self.life.disconnect("page")
        self.life.active_requests = 1
        self.time = 16
        self.assertFalse(self.life.check())
        self.life.active_requests = 0
        self.assertTrue(self.life.reserve())
        self.time = 135
        self.assertFalse(self.life.check())
        self.time = 136
        self.assertTrue(self.life.check())

    def test_manual_diagnostic_mode_does_not_auto_exit(self):
        life = BrowserLifetime(clock=lambda: self.time)
        self.time = 100000
        self.assertFalse(life.check())

    def test_websocket_disconnects_track_tabs_and_foreign_origins_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="lifetime-test-", dir=ROOT / ".tmp") as folder:
            app = create_app(Path(folder), on_idle=lambda: None, auth_required=False)
            with TestClient(app, base_url="http://127.0.0.1") as client:
                life = app.state.browser_lifetime
                with client.websocket_connect("ws://127.0.0.1/api/browser/connection", headers={"origin": "http://127.0.0.1"}) as first:
                    first.send_text("connected")
                    with client.websocket_connect("ws://127.0.0.1/api/browser/connection", headers={"origin": "http://127.0.0.1"}) as second:
                        second.send_text("connected")
                        self.assertEqual(len(life.clients), 2)
                    self.assertEqual(len(life.clients), 1)
                self.assertEqual(len(life.clients), 0)
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("ws://127.0.0.1/api/browser/connection", headers={"origin": "https://foreign.example"}):
                        pass
                self.assertEqual(client.post("/api/browser/reserve").status_code, 200)
                self.assertEqual(client.post("/api/browser/reserve", headers={"origin": "https://foreign.example"}).status_code, 403)
