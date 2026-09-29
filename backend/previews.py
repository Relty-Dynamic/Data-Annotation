from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import HTTPException
from .io_rate import read_rates

LOGGER = logging.getLogger("uvicorn.error")


@dataclass
class PreviewSpec:
    key: str
    path: Path
    source: dict
    target: Path
    error_path: Path
    thumbnail_target: Path | None = None
    legacy_preview: Path | None = None
    legacy_thumbnail: Path | None = None
    project_id: str | None = None


class PreviewManager:
    """One encoder with persistent-intent batches and a replaceable interactive priority queue."""

    def __init__(self, render: Callable):
        self.render = render
        self.condition = threading.Condition(threading.RLock())
        self.jobs: dict[str, dict] = {}
        self.blocked_projects: set[str] = set()
        self.pending: list[str] = []
        self.batches: dict[str, set[str]] = {}
        self.worker: threading.Thread | None = None
        self.stopping = threading.Event()

    def status(self, spec: PreviewSpec) -> dict:
        with self.condition:
            job = self.jobs.get(spec.key)
            if job:
                return {name: job[name] for name in ("state", "progress", "detail")}
        try:
            error = json.loads(spec.error_path.read_text(encoding="utf-8"))
            return {"state": "error", "progress": None, "detail": str(error["detail"])}
        except (OSError, ValueError, KeyError, TypeError):
            return {"state": "idle", "progress": None, "detail": "此片段尚未准备预览。"}

    def _enqueue(self, spec: PreviewSpec, retry: bool = False) -> bool:
        if spec.project_id in self.blocked_projects:
            raise HTTPException(409, "项目正在删除，不能再准备预览。")
        job = self.jobs.get(spec.key)
        if job and job["state"] == "running":
            return False
        if not retry and self.status(spec)["state"] == "error":
            return False
        if retry:
            spec.error_path.unlink(missing_ok=True)
        self.jobs[spec.key] = {"spec": spec, "cancel": threading.Event(), "state": "queued", "progress": 0, "detail": "正在排队；已有预览处理完成后会自动开始。"}
        return True

    def _start_worker(self):
        if self.pending and (self.worker is None or not self.worker.is_alive()):
            self.worker = threading.Thread(target=self._work, name="preview-encoder", daemon=True)
            self.worker.start()
        self.condition.notify_all()

    def request(self, specs: list[PreviewSpec], retry_key: str | None = None) -> None:
        with self.condition:
            if self.stopping.is_set():
                raise HTTPException(503, "预览服务正在关闭，请重新打开平台后重试。")
            wanted = {spec.key for spec in specs}
            pinned = set().union(*self.batches.values()) if self.batches else set()
            remaining = [key for key in self.pending if key in pinned and key not in wanted]
            for key in self.pending:
                if key not in wanted and key not in pinned and self.jobs[key]["state"] == "queued":
                    self.jobs.pop(key)
            self.pending = []
            for spec in specs:
                if self._enqueue(spec, retry=retry_key == spec.key) and spec.key not in self.pending:
                    self.pending.append(spec.key)
            self.pending.extend(remaining)
            self._start_worker()

    def request_batch(self, batch_id: str, specs: list[PreviewSpec], retry_failed: bool = False) -> None:
        with self.condition:
            if self.stopping.is_set():
                raise HTTPException(503, "预览服务正在关闭，请重新打开平台后重试。")
            self.batches[batch_id] = {spec.key for spec in specs}
            for spec in specs:
                if self._enqueue(spec, retry=retry_failed) and spec.key not in self.pending:
                    self.pending.append(spec.key)
            self._start_worker()

    def _work(self):
        while not self.stopping.is_set():
            with self.condition:
                if not self.pending:
                    self.worker = None
                    return
                key = self.pending.pop(0)
                job = self.jobs[key]
                spec = job["spec"]
                job.update(state="running", progress=0, detail="正在生成预览；完成后可直接复用缓存。")
            started = time.monotonic()
            LOGGER.info("Preparing preview: %s", spec.path.name)

            def update(progress):
                with self.condition:
                    job["progress"] = max(job["progress"], min(99, round(progress, 1)))
                    if job["progress"] >= 99:
                        job["detail"] = "正在完成预览封装与时长校验。"

            try:
                self.render(spec, update, job["cancel"])
                spec.error_path.unlink(missing_ok=True)
                with self.condition:
                    job.update(state="ready", progress=100, detail=None)
                LOGGER.info("Preview ready: %s (%.1fs)", spec.path.name, time.monotonic() - started)
            except Exception as exc:
                if self.stopping.is_set() or job["cancel"].is_set():
                    with self.condition:
                        job.update(state="idle", progress=None, detail="平台关闭，准备已暂停；重新打开后可以继续。")
                    LOGGER.info("Preview paused: %s", spec.path.name)
                    continue
                if isinstance(exc, HTTPException):
                    detail = str(exc.detail)
                elif isinstance(exc, OSError):
                    detail = "预览文件读写失败，请检查磁盘空间、素材位置或共享网络后重试。"
                else:
                    detail = "预览处理失败，请重试并检查本机处理日志。"
                with self.condition:
                    if not job["cancel"].is_set():
                        try:
                            temp = spec.error_path.with_suffix(".tmp")
                            temp.write_text(json.dumps({"detail": detail}, ensure_ascii=False), encoding="utf-8")
                            os.replace(temp, spec.error_path)
                        except OSError:
                            LOGGER.exception("Could not persist preview failure")
                    job.update(state="error", progress=None, detail=detail)
                LOGGER.warning("Preview failed: %s: %s", spec.path.name, detail)

    def cancel_project(self, ident: str, timeout: float = 30):
        with self.condition:
            self.blocked_projects.add(ident)
            self.batches.pop(ident, None)
            keys = {key for key, job in self.jobs.items() if job["spec"].project_id == ident}
            self.pending = [key for key in self.pending if key not in keys]
            for key in keys:
                self.jobs[key]["cancel"].set()
                if self.jobs[key]["state"] == "queued":
                    self.jobs[key]["state"] = "idle"
        deadline = time.monotonic() + timeout
        while True:
            with self.condition:
                if not any(self.jobs.get(key, {}).get("state") == "running" for key in keys):
                    for key in keys:
                        self.jobs.pop(key, None)
                    return
            if time.monotonic() >= deadline:
                raise HTTPException(409, "正在停止该项目的预览生成，请稍后重试删除。")
            time.sleep(.05)

    def close(self):
        self.stopping.set()
        with self.condition:
            self.pending.clear()
            for job in self.jobs.values():
                job["cancel"].set()
            worker = self.worker
            self.condition.notify_all()
        if worker:
            worker.join(timeout=5)


def run_ffmpeg(args: list[str], duration_ms: int, log_path: Path, update: Callable, stopping: threading.Event,
               *, stall_seconds: float = 120, timeout_seconds: float | None = None) -> None:
    """Drain progress independently of HTTP; stop stalled encoders and retain bounded diagnostics."""
    timeout_seconds = timeout_seconds or max(300, min(7200, duration_ms / 1000 * 4))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = last_progress = time.monotonic()
    last_time_us = -1
    with log_path.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                                       text=True, encoding="utf-8", errors="replace", creationflags=flags)
        except OSError as exc:
            raise HTTPException(503, f"无法启动预览处理工具：{exc.strerror or type(exc).__name__}")

        read_rates.track(process, log_path)

        def read_progress():
            nonlocal last_progress, last_time_us
            assert process.stdout is not None
            for line in process.stdout:
                name, _, value = line.strip().partition("=")
                if name == "out_time_us":
                    try:
                        out_time_us = int(value)
                    except ValueError:
                        continue
                    if out_time_us > last_time_us:
                        last_time_us = out_time_us
                        last_progress = time.monotonic()
                        update(out_time_us / (duration_ms * 10))

        reader = threading.Thread(target=read_progress, name="preview-progress", daemon=True)
        reader.start()
        failure = None
        try:
            while process.poll() is None:
                current = time.monotonic()
                if stopping.is_set():
                    failure = "预览处理因平台关闭而中断，重新打开后可点击重试。"
                elif current - started > timeout_seconds:
                    failure = "预览处理超过最长等待时间，已停止。请检查素材和磁盘后重试。"
                elif current - last_progress > stall_seconds:
                    failure = "预览连续 120 秒没有处理进度，已停止。请检查素材和共享网络后重试。"
                if failure:
                    process.kill()
                    break
                stopping.wait(0.25)
            process.wait(timeout=10)
            reader.join(timeout=2)
            if failure:
                raise HTTPException(504, failure)
            if process.returncode:
                raise HTTPException(422, f"预览处理失败，可点击重试。处理日志：{log_path}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout:
                process.stdout.close()
