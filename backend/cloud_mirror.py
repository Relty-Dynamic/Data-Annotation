"""Forward local project metadata and timeline results to an explicitly configured service."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .service import AXES, filename_for_axis


class CloudMirror:
    def __init__(self):
        origin = os.getenv("DATAMARK_CLOUD_API_ORIGIN", "").strip()
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
            raise HTTPException(503, "本机尚未配置远端同步接口地址。")
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
            raise HTTPException(503, "无法连接远端同步接口，请检查网络和接口地址。") from error
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

    @staticmethod
    def written_documents(project: dict, service, public: dict) -> tuple[list[str], dict[str, str]]:
        if project.get("draft_dirty") or not project.get("source_dir") or not project.get("last_writeback"):
            raise HTTPException(409, "本机标注尚未成功写回原目录，不能同步最终文件。")
        source_root = Path(project["source_dir"])
        if service.current_external_hashes(source_root) != service.expected_external_hashes(project):
            raise HTTPException(409, "原目录时间轴文件已变化，未同步最终结果。")
        source = service.external_directory(source_root)
        save_id = project["last_writeback"].get("save_id")
        if not isinstance(save_id, str) or not save_id:
            raise HTTPException(409, "缺少已写回批次编号，未同步最终结果。")
        archived = {}
        for axis in [*AXES, *(track["id"] for track in public["custom_tracks"])]:
            try:
                payload = (source / filename_for_axis(axis)).read_text(encoding="utf-8")
                document = json.loads(payload)
            except (OSError, UnicodeError, ValueError):
                continue
            if isinstance(document, dict) and document.get("save_id") == save_id:
                archived[axis] = (payload, document)

        def written_axes(document):
            timebase = document.get("timebase")
            if not isinstance(timebase, dict):
                return None
            return timebase.get("written_tracks", timebase.get("selected_tracks", public["selected_tracks"]))

        axes = written_axes(next(iter(archived.values()))[1]) if archived else None
        if (not isinstance(axes, list) or not axes or any(not isinstance(axis, str) for axis in axes)
                or len(set(axes)) != len(axes) or set(archived) != set(axes)
                or any(document.get("axis") != axis or written_axes(document) != axes
                       or document["timebase"].get("selected_tracks", axes) != axes
                       for axis, (_, document) in archived.items())):
            raise HTTPException(409, "已写回时间轴文件缺失或批次不一致，未同步最终结果。")
        return axes, {axis: archived[axis][0] for axis in axes}

    def sync(self, local_user_id: str, project: dict, service, *, final: bool = False) -> dict:
        with self.lock:
            session = self.sessions.get(local_user_id)
            if not session:
                raise HTTPException(409, "请先连接公网标注账号，再同步本机项目。")
            client, _, csrf = session
            public = service.public(project)
            snapshot = {key: public[key] for key in ("id", "name", "revision", "duration_ms", "fixed_tracks", "selected_tracks", "track_names", "track_labels", "custom_tracks", "annotations")}
            snapshot["selected_tracks"] = [axis for axis in snapshot["selected_tracks"] if axis != "habit"]
            snapshot["source_folder_name"] = Path(project["source_dir"]).name if project.get("source_dir") else public["name"]
            snapshot["videos"] = [{key: video.get(key) for key in ("id", "name", "start_ms", "end_ms", "duration_ms", "recording_start")}
                                  for video in public["videos"]]
            snapshot["submitted_at"] = (project.get("last_writeback") or {}).get("saved_at") if not project.get("draft_dirty") else None
            documents = None
            if final:
                axes, documents = self.written_documents(project, service, public)
                snapshot["selected_tracks"] = axes
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
            try:
                result = response.json()
            except ValueError as error:
                raise HTTPException(503, "Ubuntu 返回的结果无效，未能确认 NAS 保存成功。") from error
            if not isinstance(result, dict):
                raise HTTPException(503, "Ubuntu 返回的结果无效，未能确认 NAS 保存成功。")
            if final and (result.get("has_documents") is not True or not result.get("nas_relative_path")):
                raise HTTPException(409, "Ubuntu 接口尚未确认 NAS 写回，请升级服务并重新同步；本机文件已保留。")
            return result
