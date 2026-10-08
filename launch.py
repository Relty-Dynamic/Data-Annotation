"""Start DataMark silently; the last browser tab controls the normal server lifetime."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:8765"

def health():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{BASE_URL}/api/health", timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None

def reserve_browser():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(BASE_URL + "/api/browser/reserve", data=b"", method="POST")
        with opener.open(request, timeout=2) as response:
            return response.status == 200
    except OSError:
        return False

def open_browser():
    if os.name == "nt":
        try:
            os.startfile(BASE_URL)
            return
        except OSError:
            pass
    if not webbrowser.open(BASE_URL):
        raise OSError("无法打开默认浏览器，请手动访问 " + BASE_URL)


def reuse_running(args):
    current = health()
    if not current or current.get("application") != "datamark" or current.get("stopping") or "account-login" not in current.get("capabilities", []):
        return False
    if sys.platform == "darwin" and "native-file-picker" not in current.get("capabilities", []):
        return False
    if not args.no_browser and current.get("browser_lifetime") and not reserve_browser():
        return False
    if not args.no_browser and not args.no_open:
        open_browser()
    return True


def bind_listener(port=8765):
    listener = socket.socket()
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))
        return listener
    except BaseException:
        listener.close()
        raise


def open_when_ready(server, stop):
    # The first startup can spend time checking offline shared folders.
    while not stop.is_set():
        if server.started:
            open_browser()
            return
        stop.wait(.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="Manual diagnostic mode; keep running without a page")
    parser.add_argument("--no-open", action="store_true", help="Reuse an already-open page while keeping browser-controlled lifetime")
    args = parser.parse_args()
    settings = {
        "PYTHONNOUSERSITE": "1", "PIP_REQUIRE_VIRTUALENV": "1",
        "PIP_CACHE_DIR": str(ROOT / ".cache" / "pip"),
        "NPM_CONFIG_CACHE": str(ROOT / ".cache" / "npm"),
        "TEMP": str(ROOT / ".tmp"), "TMP": str(ROOT / ".tmp"),
    }
    for folder in (".tmp", ".cache/pip", ".cache/npm", ".local/logs"):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    os.environ.update(settings)
    os.chdir(ROOT)
    from backend.auth import AuthStore
    if not AuthStore(ROOT).has_admin():
        raise RuntimeError("尚未创建管理员账号。请先在项目虚拟环境运行 python -m backend.auth，然后重启平台。")
    current = health()
    if current and current.get("application") == "datamark" and "account-login" not in current.get("capabilities", []):
        raise RuntimeError("端口 8765 仍由旧版无登录服务占用。请先关闭旧平台的所有网页，等待服务退出后重新启动。")
    if sys.platform == "darwin" and current and current.get("application") == "datamark" and "native-file-picker" not in current.get("capabilities", []):
        raise RuntimeError("端口 8765 仍由不支持 macOS 文件选择的旧服务占用。请先关闭旧平台的所有网页，等待服务退出后重新启动新版。")
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, (ROOT / ".local" / "logs" / f"server.{name}.log").open("a", encoding="utf-8", buffering=1))
    if reuse_running(args):
        return 0
    # Keep the port claimed while the app initializes. A second shortcut click
    # waits for this launcher instead of starting another copy of the server.
    deadline = time.monotonic() + 300
    while True:
        try:
            listener = bind_listener()
            break
        except OSError:
            if reuse_running(args):
                return 0
            if time.monotonic() >= deadline:
                raise RuntimeError("端口 8765 长时间被占用，请检查后台服务或手动打开 " + BASE_URL)
            time.sleep(.2)
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("页面尚未构建，请先在 Windows 运行 setup.cmd，或在 macOS 的项目目录构建前端。")
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("请使用项目内虚拟环境启动平台。Windows 可使用启动快捷方式，macOS 可运行 .venv/bin/python launch.py。")
    import uvicorn
    from backend.app import create_app
    server = None
    def stop_when_browser_closes():
        if server:
            server.should_exit = True
    app = create_app(ROOT, on_idle=None if args.no_browser else stop_when_browser_closes)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, use_colors=False, ws="websockets-sansio", timeout_graceful_shutdown=10))
    pid_file = ROOT / ".local" / "server.pid"
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    stop_open = threading.Event()
    if not args.no_browser and not args.no_open:
        threading.Thread(target=open_when_ready, args=(server, stop_open), daemon=True).start()
    try:
        server.run(sockets=[listener])
    finally:
        stop_open.set()
        listener.close()
        if pid_file.exists() and pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
            pid_file.unlink()
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        if os.name == "nt" and Path(sys.executable).name.lower() == "pythonw.exe":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc) + "\n详细日志：" + str(ROOT / ".local/logs/server.stderr.log"), "DataMark 启动失败", 0x10)
        raise SystemExit(1)
