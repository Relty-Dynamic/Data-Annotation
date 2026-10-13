"""Start DataMark silently; the last browser tab controls the normal server lifetime."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:8765"

def owned_windows_process(pid):
    query = f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object Name,CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
                            capture_output=True, text=True, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    process = json.loads(result.stdout) if result.stdout.strip() else {}
    if not isinstance(process, dict):
        return False
    script_argument = r'(?:^|[\s"])' + re.escape(str(ROOT / "launch.py")) + r'(?=$|[\s"])'
    return (process.get("Name", "").lower() in {"python.exe", "pythonw.exe"}
            and re.search(script_argument, process.get("CommandLine") or "", re.IGNORECASE) is not None)


def restart_running():
    """Stop only the server recorded by this checkout before starting anew."""
    current = health()
    if not current:
        return
    if current.get("application") != "datamark":
        raise RuntimeError("8765 端口由其他应用占用，未停止该应用。")
    pid_file = ROOT / ".local/server.pid"
    if not pid_file.is_file():
        raise RuntimeError("当前服务没有本项目的进程记录，无法自动重启。")
    value = pid_file.read_text(encoding="ascii").strip()
    if not value.isdecimal() or int(value) <= 0:
        raise RuntimeError("本机后端进程记录无效。")
    pid = int(value)
    if os.name != "nt":
        raise RuntimeError("自动重启入口仅用于 Windows 本机。")
    # A stale PID file must never stop a different program or checkout.
    if not owned_windows_process(pid):
        raise RuntimeError("进程记录与本项目启动器不符，未停止任何进程。")
    stop_file = ROOT / ".local/server.stop"
    stop_file.write_text(value, encoding="ascii")
    # Updated launchers exit gracefully. Older launchers do not watch the file;
    # only those verified above need a one-time process stop.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not health():
            break
        time.sleep(.25)
    else:
        if not owned_windows_process(pid):
            raise RuntimeError("旧进程身份已变更，未停止任何其他进程。")
        subprocess.run(["taskkill.exe", "/PID", value, "/T", "/F"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 30
    while health():
        if time.monotonic() >= deadline:
            raise RuntimeError("旧服务仍在退出，请稍后重试。")
        time.sleep(.25)
    stop_file.unlink(missing_ok=True)

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
    mock_mode = os.getenv("DATAMARK_S3_MOCK", "").strip() == "1"
    if ("s3-mock" in current.get("capabilities", [])) != mock_mode:
        return False
    if sys.platform == "darwin" and not mock_mode and "native-file-picker" not in current.get("capabilities", []):
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
    parser.add_argument("--restart", action="store_true", help="Restart the server owned by this Windows checkout")
    args = parser.parse_args()
    settings = {
        "PYTHONNOUSERSITE": "1", "PIP_REQUIRE_VIRTUALENV": "1",
        "PIP_CACHE_DIR": str(ROOT / ".cache" / "pip"),
        "NPM_CONFIG_CACHE": str(ROOT / ".cache" / "npm"),
        "TEMP": str(ROOT / ".tmp"), "TMP": str(ROOT / ".tmp"), "TMPDIR": str(ROOT / ".tmp"),
    }
    for folder in (".tmp", ".cache/pip", ".cache/npm", ".local/logs"):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    os.environ.update(settings)
    os.chdir(ROOT)
    from backend.auth import AuthStore
    mock_mode = os.getenv("DATAMARK_S3_MOCK", "").strip() == "1"
    auth_root = ROOT / ".local" / "s3-mock-app" if mock_mode else ROOT
    if not AuthStore(auth_root).has_admin():
        command = "python -m backend.auth --root .local/s3-mock-app" if mock_mode else "python -m backend.auth"
        raise RuntimeError(f"尚未创建管理员账号。请先在项目虚拟环境运行 {command}，然后重启平台。")
    if args.restart:
        restart_running()
    current = health()
    if current and current.get("application") == "datamark" and "account-login" not in current.get("capabilities", []):
        raise RuntimeError("端口 8765 仍由旧版无登录服务占用。请先关闭旧平台的所有网页，等待服务退出后重新启动。")
    if current and current.get("application") == "datamark" and ("s3-mock" in current.get("capabilities", [])) != mock_mode:
        raise RuntimeError("端口 8765 正在运行另一种模式。请先关闭旧平台的所有网页并等待服务退出。")
    if sys.platform == "darwin" and not mock_mode and current and current.get("application") == "datamark" and "native-file-picker" not in current.get("capabilities", []):
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
    stop_file = ROOT / ".local/server.stop"
    def stop_when_requested():
        while not stop_open.wait(.25):
            try:
                if stop_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                    server.should_exit = True
                    return
            except FileNotFoundError:
                pass
    threading.Thread(target=stop_when_requested, daemon=True).start()
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
