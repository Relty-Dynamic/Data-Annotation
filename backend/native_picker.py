"""Native path selection for the local server; video bytes never pass through the browser."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import HTTPException


_MAC_PICKER_SCRIPT = r"""
function run(argv) {
    var app = Application.currentApplication();
    app.includeStandardAdditions = true;
    try {
        var selected = argv[0] === "directory"
            ? [app.chooseFolder({withPrompt: "选择原视频目录（原视频保留在此处）"})]
            : app.chooseFile({withPrompt: "选择视频（直接读取，不复制）", multipleSelectionsAllowed: true});
        if (!Array.isArray(selected)) selected = [selected];
        return JSON.stringify({paths: selected.map(function(item) { return item.toString(); })});
    } catch (error) {
        return JSON.stringify(error.errorNumber === -128 ? {paths: []} : {error: true});
    }
}
"""


def native_picker_available() -> bool:
    return os.name == "nt" or sys.platform == "darwin"


def choose_local_paths(root: Path, kind: str) -> list[str]:
    if kind not in {"files", "directory"}:
        raise HTTPException(422, "请选择视频文件或目录。")
    if not native_picker_available():
        raise HTTPException(503, "当前系统不支持本机选择窗口，请填写素材完整路径。")
    result_path = None
    try:
        if os.name == "nt":
            temporary = root.resolve() / ".tmp"
            temporary.mkdir(exist_ok=True)
            result_path = temporary / ("file-picker-" + uuid.uuid4().hex + ".json")
            # The helper owns its Tk event loop on its main thread. CREATE_NO_WINDOW
            # hides the Python console while the picker is open on Windows.
            completed = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()), kind, str(result_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=900,
            )
            if completed.returncode or not result_path.is_file():
                raise HTTPException(503, "未能打开本机选择窗口，请直接填写视频文件或目录的完整路径。")
            result = json.loads(result_path.read_text(encoding="utf-8"))
        else:
            # Apple's built-in dialog works even when the local Python lacks a usable Tk runtime.
            completed = subprocess.run(
                ["/usr/bin/osascript", "-l", "JavaScript", "-e", _MAC_PICKER_SCRIPT, kind],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=900,
            )
            if completed.returncode:
                raise HTTPException(503, "未能打开本机选择窗口，请直接填写视频文件或目录的完整路径。")
            result = json.loads(completed.stdout)
        if not isinstance(result, dict) or result.get("error"):
            raise HTTPException(503, "未能读取文件选择结果，请直接填写素材完整路径。")
        paths = result.get("paths")
        if not isinstance(paths, list) or any(not isinstance(path, str) or not path for path in paths):
            raise HTTPException(503, "文件选择结果无效，请重新选择。")
        return paths
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "文件选择窗口已超时，请重新打开选择窗口或填写素材路径。")
    except (OSError, ValueError):
        raise HTTPException(503, "无法使用本机选择窗口，请填写素材完整路径。")
    finally:
        if result_path is not None:
            result_path.unlink(missing_ok=True)


def _dialog(kind: str, result_path: Path) -> None:
    import tkinter as tk
    from tkinter import filedialog
    window = None
    try:
        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        if kind == "directory":
            selected = filedialog.askdirectory(parent=window, title="选择原视频目录（原视频保留在此处）", mustexist=True)
            paths = [selected] if selected else []
        else:
            paths = list(filedialog.askopenfilenames(parent=window, title="选择视频（直接读取，不复制）",
                filetypes=[("视频文件", "*.mp4 *.avi *.mov *.mkv *.webm *.m4v *.mts *.m2ts *.mpg *.mpeg *.wmv"), ("所有文件", "*.*")]))
        result = {"paths": paths}
    except Exception:
        result = {"error": True}
    finally:
        if window is not None:
            window.destroy()
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    _dialog(sys.argv[1], Path(sys.argv[2]))
