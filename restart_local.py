"""Windows desktop entry: rebuild the UI, restart the local server, open a page."""
from __future__ import annotations

import ctypes
import errno
import msvcrt
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

import launch

ROOT = Path(__file__).resolve().parent


def rebuild_frontend(log):
    node = shutil.which("node.exe")
    npm = shutil.which("npm.cmd")
    if not node or not npm:
        raise RuntimeError("未找到本机 Node.js / npm，请先运行 setup.cmd。")
    npm_cli = Path(npm).parent / "node_modules/npm/bin/npm-cli.js"
    if not npm_cli.is_file() or not (ROOT / "frontend/node_modules/typescript").is_dir():
        raise RuntimeError("前端依赖尚未准备好，请先运行 setup.cmd。")
    result = subprocess.run([node, str(npm_cli), "run", "build"], cwd=ROOT / "frontend",
                            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("前端构建失败，未重启后台。请查看启动日志。")


def start(log, *, open_page=True):
    python = ROOT / ".venv/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("项目虚拟环境不存在，请先运行 setup.cmd。")
    # Complete the build before stopping the current service.
    rebuild_frontend(log)
    pid_file = ROOT / ".local/server.pid"
    old_pid = pid_file.read_text(encoding="ascii").strip() if pid_file.is_file() else ""
    process = subprocess.Popen([str(python), "-u", str(ROOT / "launch.py"), "--restart", "--no-open"],
                               cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = launch.health()
        new_pid = pid_file.read_text(encoding="ascii").strip() if pid_file.is_file() else ""
        if (current and current.get("application") == "datamark" and not current.get("stopping")
                and new_pid and new_pid != old_pid):
            if open_page:
                if not launch.reserve_browser():
                    raise RuntimeError("后台已启动，但无法为网页保留连接，请稍后重试。")
                launch.open_browser()
            return
        if process.poll() is not None:
            raise RuntimeError("本机后台启动失败，请查看启动日志。")
        time.sleep(.25)
    raise RuntimeError("本机后台启动超过 120 秒，请查看启动日志。")


def main():
    os.chdir(ROOT)
    for folder in (".tmp", ".cache/pip", ".cache/npm", ".local/logs"):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "PYTHONNOUSERSITE": "1", "PIP_REQUIRE_VIRTUALENV": "1",
        "PIP_CACHE_DIR": str(ROOT / ".cache/pip"), "NPM_CONFIG_CACHE": str(ROOT / ".cache/npm"),
        "TEMP": str(ROOT / ".tmp"), "TMP": str(ROOT / ".tmp"), "TMPDIR": str(ROOT / ".tmp"),
    })
    log_path = ROOT / ".local/logs/restart-local.log"
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        try:
            # Repeated clicks during a build wait and then just open the page.
            with (ROOT / ".local/restart-local.lock").open("a+b") as lock:
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b"0")
                    lock.flush()
                waited = False
                deadline = time.monotonic() + 180
                while True:
                    lock.seek(0)
                    try:
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                            raise
                        waited = True
                        if time.monotonic() >= deadline:
                            raise RuntimeError("另一个启动操作仍在进行，请稍后重试。")
                        time.sleep(.5)
                try:
                    print(time.strftime("\n[%Y-%m-%d %H:%M:%S] Local startup/restart"), file=log)
                    current = launch.health() if waited else None
                    if current and current.get("application") == "datamark" and not current.get("stopping"):
                        if "--no-open" not in sys.argv:
                            if not launch.reserve_browser():
                                raise RuntimeError("无法为网页保留连接，请稍后重试。")
                            launch.open_browser()
                    else:
                        start(log, open_page="--no-open" not in sys.argv)
                    print("Local frontend and backend are ready.", file=log)
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception as error:
            traceback.print_exc(file=log)
            if "--no-open" not in sys.argv:
                ctypes.windll.user32.MessageBoxW(None, f"{error}\n\n日志：{log_path}", "DataMark 启动失败", 0x10)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
