"""Forward local project metadata and timeline results to the authenticated Ubuntu platform."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .service import AXES, FILENAMES


class CloudMirror:
    def __init__(self):
        origin = os.getenv("DATAMARK_CLOUD_API_ORIGIN", "https://api-annotate.reltydynamic.com:10443").strip()
        if origin:
            parsed = urlsplit(origin)
            port = parsed.port
            if (parsed.scheme != "https" or not parsed.hostname or port == 443 or parsed.path
                    or parsed.query or parsed.fragment or parsed.username or parsed.password
                    or parsed.netloc != parsed.netloc.lower()):
                raise ValueError("DATAMARK_CLOUD_API_ORIGIN must be a plain HTTPS origin")
        self.origin = origin
        self.lock = threading.RLock()
        self.sessions: dict[str, tuple[httpx.Client, dict, str]] = {}

    def status(self, local_user_id: str) -> dict:
        with self.lock:
            session = self.sessions.get(local_user_id)
        return {"configured": bool(self.origin), "connected": session is not None,
                "user": session[1] if session else None}

    def connect(self, local_user_id: str, username: str, password: str) -> dict:
        if not self.origin:
            raise HTTPException(503, "本机尚未配置 Ubuntu 公网接口地址。")
        client = httpx.Client(base_url=self.origin, timeout=30)
        try:
            health = client.get("/api/health")
            if health.status_code != 200 or health.json().get("application") != "datamark":
                raise HTTPException(503, "公网接口不是 DataMark 标注平台。")
            response = client.post("/api/auth/login", json={"username": username, "password": password})
            if response.status_code != 200:
                raise HTTPException(401, "公网账号登录失败，请核对账号和密码。")
            body = response.json()
            user, csrf = body.get("user"), body.get("csrf")
            if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not isinstance(csrf, str):
                raise HTTPException(503, "公网账号会话格式不正确。")
        except (httpx.HTTPError, ValueError) as error:
            client.close()
            raise HTTPException(503, "无法连接 Ubuntu 公网接口，请检查网络和接口地址。") from error
        except HTTPException:
            client.close()
            raise
        with self.lock:
            old = self.sessions.pop(local_user_id, None)
            self.sessions[local_user_id] = (client, user, csrf)
        if old:
            old[0].close()
        return self.status(local_user_id)

    def disconnect(self, local_user_id: str) -> None:
        with self.lock:
            old = self.sessions.pop(local_user_id, None)
        if old:
            old[0].close()

    def sync(self, local_user_id: str, project: dict, service, *, final: bool = False) -> dict:
        with self.lock:
            session = self.sessions.get(local_user_id)
            if not session:
                raise HTTPException(409, "请先连接公网标注账号，再同步本机项目。")
            client, _, csrf = session
            public = service.public(project)
            snapshot = {key: public[key] for key in ("id", "name", "revision", "duration_ms", "fixed_tracks", "track_labels", "custom_tracks", "annotations")}
            snapshot["source_folder_name"] = Path(project["source_dir"]).name if project.get("source_dir") else public["name"]
            snapshot["videos"] = [{key: video.get(key) for key in ("id", "name", "start_ms", "end_ms", "duration_ms", "recording_start")}
                                  for video in public["videos"]]
            snapshot["submitted_at"] = (project.get("last_writeback") or {}).get("saved_at") if not project.get("draft_dirty") else None
            documents = None
            if final:
                if project.get("draft_dirty") or not project.get("source_dir") or not project.get("last_writeback"):
                    raise HTTPException(409, "本机标注尚未成功写回原目录，不能同步最终文件。")
                source_root = Path(project["source_dir"])
                source = service.external_directory(source_root)
                if service.current_external_hashes(source_root) != service.expected_external_hashes(project):
                    raise HTTPException(409, "原目录时间轴文件已变化，未同步最终结果。")
                axes = (*AXES, *(item["id"] for item in project.get("custom_tracks", [])))
                documents = {axis: (source / FILENAMES.get(axis, axis + ".timeline.json")).read_text(encoding="utf-8") for axis in axes}
            try:
                response = client.put("/api/local-reports", json={"snapshot": snapshot, "documents": documents},
                                      headers={"X-CSRF-Token": csrf})
            except httpx.HTTPError as error:
                raise HTTPException(503, "本机项目已保存，但同步 Ubuntu 失败；请联网后重试。") from error
            if response.status_code == 401:
                raise HTTPException(401, "公网账号会话已过期，请重新连接后同步。")
            if response.status_code != 200:
                try:
                    detail = response.json().get("detail")
                except ValueError:
                    detail = None
                raise HTTPException(response.status_code, detail if isinstance(detail, str) else "Ubuntu 拒绝了本机项目同步。")
            result = response.json()
            if final and (result.get("has_documents") is not True or not result.get("nas_relative_path")):
                raise HTTPException(409, "Ubuntu 接口尚未确认 NAS 写回，请升级服务并重新同步；本机文件已保留。")
            return result
