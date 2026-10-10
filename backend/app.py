from __future__ import annotations

import os
import re
import uuid
import hmac
import hashlib
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from .service import ProjectService
from .browser_lifetime import BrowserLifetime
from .native_picker import choose_local_paths, native_picker_available
from .source_browser import browse_sources
from .media_response import CancellableFileResponse
from .auth import AuthStore
from .local_reports import LocalReportStore
from .cloud_mirror import CloudMirror
from .mock_s3 import MockProjectService, MockS3Catalog, MockS3Store


class OpenRequest(BaseModel):
    path: str = Field(min_length=1, max_length=32767)
    name: str | None = Field(default=None, strict=True)


class SupplementRequest(OpenRequest):
    expected_revision: int = Field(ge=0, strict=True)


class FilesRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=1000)
    name: str | None = Field(default=None, strict=True)


class SupplementFilesRequest(FilesRequest):
    expected_revision: int = Field(ge=0, strict=True)


class PickFilesRequest(BaseModel):
    kind: Literal["files", "directory"] = "files"


class DraftRequest(BaseModel):
    annotations: dict
    expected_revision: int = Field(ge=0, strict=True)


class WritebackRequest(BaseModel):
    expected_revision: int = Field(ge=0, strict=True)


class SubmittedCacheRequest(WritebackRequest):
    save_id: str = Field(min_length=1, max_length=128)


class EditingSessionRequest(BaseModel):
    tab_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")


class FixedTrackRequest(BaseModel):
    enabled: bool
    expected_revision: int = Field(ge=0, strict=True)


class TrackLabelsRequest(BaseModel):
    labels: list[str]
    expected_revision: int = Field(ge=0, strict=True)


class LocalReportRequest(BaseModel):
    snapshot: dict
    documents: dict[str, str] | None = None


class CloudSyncRequest(BaseModel):
    final: bool = False


class CustomTrackRequest(BaseModel):
    name: str = Field(strict=True)
    mode: Literal["state", "event"]
    labels: list[str] = Field(default_factory=list)
    expected_revision: int = Field(ge=0, strict=True)


class RenameProjectRequest(BaseModel):
    name: str = Field(strict=True)
    expected_revision: int = Field(ge=0, strict=True)


class PreparationRequest(BaseModel):
    retry_failed: bool = False
    cache_generation: int | None = Field(default=None, ge=0, strict=True)


class DeleteProjectRequest(BaseModel):
    confirmed: Literal[True]
    expected_revision: int = Field(ge=0, strict=True)


class PreviewRequest(BaseModel):
    prefetch: bool = True
    retry: bool = False


class SkipFailedRequest(DeleteProjectRequest):
    video_ids: list[str] = Field(min_length=1, max_length=1000)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


class UserRequest(LoginRequest):
    display_name: str = Field(min_length=1, max_length=80)


class AssignmentRequest(BaseModel):
    user_id: str | None = None


class ActiveRequest(BaseModel):
    active: bool


class PasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


class ChangePasswordRequest(PasswordRequest):
    current_password: str


def create_app(root: Path | None = None, on_idle=None, *, auth_required: bool = True,
               source_root: Path | None = None) -> FastAPI:
    root = root or Path(os.getenv("DATAMARK_ROOT", str(Path(__file__).resolve().parents[1])))
    configured_origin = os.getenv("DATAMARK_ORIGIN", "").strip()
    public_origin = os.getenv("DATAMARK_PUBLIC_ORIGIN", "").strip()
    public_api_origin = os.getenv("DATAMARK_PUBLIC_API_ORIGIN", "").strip()
    mock_mode = os.getenv("DATAMARK_S3_MOCK", "").strip() == "1"
    data_root = root / ".local" / "s3-mock-app" if mock_mode else root
    mock_source = os.getenv("DATAMARK_S3_MOCK_SOURCE_DIR", "").strip()
    mock_catalog = (MockS3Catalog(Path(mock_source).expanduser(),
                                  os.getenv("DATAMARK_S3_MOCK_SOURCE_KEY", "").strip() or f"daily/{Path(mock_source).name}")
                    if mock_mode and mock_source else None)
    nas_source_root = source_root or Path(os.getenv("DATAMARK_SOURCE_ROOT", "/mnt/nas/homes/datacollection"))
    allowed_hosts = ["127.0.0.1", "localhost", "[::1]"]
    allowed_origins: set[str] = set()

    def validate_https_origin(origin: str, setting: str, *, dns_hostname: bool = False,
                              allow_port: bool = False) -> str:
        parsed_origin = urlsplit(origin)
        if (parsed_origin.scheme != "https" or not parsed_origin.hostname
                or parsed_origin.username or parsed_origin.password
                or parsed_origin.path or parsed_origin.query or parsed_origin.fragment
                or parsed_origin.netloc != parsed_origin.netloc.lower()
                or "*" in parsed_origin.netloc or any(char.isspace() for char in origin)):
            raise ValueError(f"{setting} must be an HTTPS origin without a path or credentials")
        port = parsed_origin.port  # Reject invalid or out-of-range ports.
        if port is not None and (not allow_port or port == 443):
            raise ValueError(f"{setting} must not include this port")
        hostname = parsed_origin.hostname
        if dns_hostname:
            labels = hostname.split(".")
            if (len(labels) < 2 or len(hostname) > 253
                    or any(not 1 <= len(label) <= 63 or not label[0].isalnum() or not label[-1].isalnum()
                           or any(not (character.isascii() and (character.isalnum() or character == "-"))
                                  for character in label) for label in labels)):
                raise ValueError(f"{setting} must use a valid DNS hostname")
        return hostname

    if configured_origin:
        bind_ip = os.getenv("DATAMARK_BIND_IP", "").strip()
        if bind_ip:
            address = ipaddress.ip_address(bind_ip)
            private_ranges = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                              ipaddress.ip_network("192.168.0.0/16"))
            if not any(address in network for network in private_ranges):
                raise ValueError("DATAMARK_BIND_IP must be an RFC1918 LAN address")
        allowed_hosts.append(validate_https_origin(configured_origin, "DATAMARK_ORIGIN"))
        allowed_origins.add(configured_origin)
    if public_origin or public_api_origin:
        if not configured_origin or not public_origin or not public_api_origin:
            raise ValueError("public frontend and API origins require DATAMARK_ORIGIN and each other")
        frontend_host = validate_https_origin(public_origin, "DATAMARK_PUBLIC_ORIGIN", dns_hostname=True)
        api_host = validate_https_origin(public_api_origin, "DATAMARK_PUBLIC_API_ORIGIN",
                                         dns_hostname=True, allow_port=True)
        if frontend_host == api_host:
            raise ValueError("public frontend and API must use separate hostnames")
        allowed_hosts.append(api_host)
        allowed_origins.add(public_origin)
    mock_s3 = MockS3Store(data_root) if mock_mode else None
    service = MockProjectService(data_root, mock_s3) if mock_s3 else ProjectService(root)
    auth = AuthStore(data_root)
    local_reports = LocalReportStore(auth, nas_source_root if configured_origin and not mock_mode else None)
    cloud_mirror = CloudMirror() if not configured_origin and not mock_mode else None
    lifetime = BrowserLifetime(on_idle)

    def require_nas_source(value: str) -> None:
        if mock_mode:
            raise HTTPException(404, "S3 mock 项目只接受测试视频上传。")
        if not configured_origin:
            return
        candidate = Path(value.strip().strip('"')).expanduser()
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise HTTPException(422, "请选择 NAS 挂载目录内的素材。")
        try:
            base = nas_source_root.resolve(strict=True)
            target = candidate.resolve(strict=True)
        except OSError:
            raise HTTPException(422, "无法访问 NAS 素材，请检查挂载和目录权限。")
        if not base.is_dir() or not target.is_relative_to(base):
            raise HTTPException(403, "只能导入 NAS 挂载目录内的素材。")

    @asynccontextmanager
    async def lifespan(app):
        if configured_origin and not auth.has_admin():
            raise RuntimeError("请先创建管理员账号，再启动 Ubuntu 内网服务。")
        service.migrate_legacy_projects()
        lifetime.start()
        try:
            yield
        finally:
            await lifetime.close()
            service.sessions.close()
            service.previews.close()
            if service.remote:
                service.remote.close()

    app = FastAPI(title="日常行为视频标注台", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.service = service
    app.state.auth = auth
    app.state.local_reports = local_reports
    app.state.cloud_mirror = cloud_mirror
    app.state.mock_s3 = mock_s3
    app.state.mock_catalog = mock_catalog
    app.state.browser_lifetime = lifetime
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        path = request.url.path
        if auth_required and path.startswith("/api/") and path not in {"/api/health", "/api/auth/login", "/api/browser/reserve"}:
            session = auth.session(request.cookies.get("datamark_session"))
            if not session:
                return JSONResponse(status_code=401, content={"detail": "请先登录。"}, headers={"Cache-Control": "no-store"})
            user, csrf_hash = session
            request.state.user = user
            request.state.csrf_hash = csrf_hash
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                csrf = request.headers.get("x-csrf-token", "")
                if not csrf or not hmac.compare_digest(hashlib.sha256(csrf.encode()).hexdigest(), csrf_hash) or csrf != request.cookies.get("datamark_csrf"):
                    return JSONResponse(status_code=403, content={"detail": "页面安全令牌已失效，请刷新页面。"})
            admin_only = path in {"/api/local-files/pick", "/api/projects/upload", "/api/users"}
            admin_only = admin_only or path.startswith("/api/users/")
            match = (None if path in {"/api/projects/open", "/api/projects/files", "/api/projects/upload"} else re.match(r"^/api/projects/([^/]+)(?:/|$)", path)) or re.match(r"^/api/(?:previews|storyboards|media|thumbnails|session-media|session-thumbnails|session-storyboards)/([^/]+)(?:/|$)", path)
            if match:
                project_id = match.group(1)
                if not auth.allowed(user, project_id):
                    return JSONResponse(status_code=403, content={"detail": "未获分配此项目。"})
                suffix = path[len("/api/projects/" + project_id):] if path.startswith("/api/projects/") else ""
                if suffix in {"/name", "/preview-cache/clear", "/assignment"} or suffix.startswith("/sources/") or (suffix == "" and path.startswith("/api/projects/") and request.method == "DELETE"):
                    admin_only = True
            if admin_only and user["role"] != "admin":
                return JSONResponse(status_code=403, content={"detail": "此操作仅管理员可执行。"})
        # The deployed HTTPS origin is explicit, so TLS termination cannot make
        # the backend's internal HTTP scheme invalidate browser requests.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site == "cross-site":
                return JSONResponse(status_code=403, content={"detail": "仅允许标注页面修改数据。"})
            if origin:
                expected_origins = allowed_origins or {f"{request.url.scheme}://{request.headers.get('host', '')}"}
                if origin not in expected_origins:
                    return JSONResponse(status_code=403, content={"detail": "仅允许标注页面修改数据。"})
        tracked = request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}
        if tracked:
            lifetime.active_requests += 1
        try:
            response = await call_next(request)
        finally:
            if tracked:
                lifetime.active_requests -= 1
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        if request.url.path.startswith(("/api/auth/", "/api/users", "/api/projects", "/api/previews", "/api/media/", "/api/thumbnails/", "/api/session-media/", "/api/session-thumbnails/", "/api/session-storyboards/", "/api/storyboards/")):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path in {"/", "/index.html"}:
            # Every reopening must check the current UI after a local update.
            response.headers["Cache-Control"] = "no-cache"
        elif response.status_code in {200, 304} and re.fullmatch(r"/assets/[^/]+-[A-Za-z0-9_-]{8,}\.(?:js|css)", request.url.path):
            # Vite changes these names when content changes; reuse them freely.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.exception_handler(OSError)
    async def filesystem_error(request: Request, exc: OSError):
        return JSONResponse(status_code=500, content={"detail": "文件读写失败，请检查磁盘空间、目录权限或共享网络。本机已保存的草稿仍保留。"})

    @app.exception_handler(RequestValidationError)
    async def request_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "请求内容格式不正确，请检查目录路径、上传文件或草稿版本。"})

    @app.get("/api/health")
    def health():
        capabilities = ["source-local-cache", "supplement-import", "compact-local-playback", "direct-compact-preparation", "parallel-compact-preparation", "four-axis-annotations", "project-naming", "account-login"]
        if mock_mode:
            capabilities.append("s3-mock")
            if mock_catalog:
                capabilities.append("s3-mock-catalog")
        else:
            capabilities.append("remote-nas-processing")
        if cloud_mirror and cloud_mirror.origin:
            capabilities.append("cloud-local-sync")
        if native_picker_available() and not configured_origin and not mock_mode:
            capabilities.append("native-file-picker")
        if configured_origin and not mock_mode:
            capabilities.append("nas-source-browser")
        return {"status": "ok", "application": "datamark", "browser_lifetime": bool(on_idle), "stopping": lifetime.stopping, "ffmpeg": bool(service.tool("ffmpeg")), "ffprobe": bool(service.tool("ffprobe")), "remote_processing": service.remote is not None, "capabilities": capabilities}

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request):
        user, token, csrf = auth.login(body.username, body.password, request.client.host if request.client else "unknown")
        response = JSONResponse({"user": user, "csrf": csrf})
        secure = bool(configured_origin) or request.url.scheme == "https"
        response.set_cookie("datamark_session", token, httponly=True, secure=secure, samesite="strict", max_age=12 * 3600, path="/")
        response.set_cookie("datamark_csrf", csrf, httponly=False, secure=secure, samesite="strict", max_age=12 * 3600, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/auth/me")
    def me(request: Request):
        csrf = request.cookies.get("datamark_csrf", "")
        if not csrf or not hmac.compare_digest(hashlib.sha256(csrf.encode()).hexdigest(), request.state.csrf_hash):
            raise HTTPException(401, "登录状态不完整，请重新登录。")
        return {**request.state.user, "csrf": csrf}

    @app.post("/api/auth/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get("datamark_session"))
        if cloud_mirror and auth_required:
            cloud_mirror.disconnect(request.state.user["id"])
        response = JSONResponse({"ok": True})
        response.delete_cookie("datamark_session", path="/")
        response.delete_cookie("datamark_csrf", path="/")
        return response

    @app.get("/api/cloud/status")
    def cloud_status(request: Request):
        if not cloud_mirror:
            raise HTTPException(404, "Ubuntu 服务不需要连接公网账号。")
        return cloud_mirror.status(request.state.user["id"])

    @app.post("/api/cloud/connect")
    def cloud_connect(body: LoginRequest, request: Request):
        if not cloud_mirror:
            raise HTTPException(404, "Ubuntu 服务不需要连接公网账号。")
        return cloud_mirror.connect(request.state.user["id"], body.username, body.password)

    @app.post("/api/cloud/projects/{project_id}/sync")
    def cloud_sync(project_id: str, body: CloudSyncRequest, request: Request):
        if not cloud_mirror:
            raise HTTPException(404, "Ubuntu 服务不需要同步本机项目。")
        project = service.load(project_id)
        if not auth.allowed(request.state.user, project_id):
            raise HTTPException(403, "未获分配此本机项目。")
        return cloud_mirror.sync(request.state.user["id"], project, service, final=body.final)

    @app.post("/api/auth/password")
    def change_password(body: ChangePasswordRequest, request: Request):
        auth.change_password(request.state.user["id"], body.current_password, body.password)
        response = JSONResponse({"ok": True})
        response.delete_cookie("datamark_session", path="/")
        response.delete_cookie("datamark_csrf", path="/")
        return response

    @app.get("/api/users")
    def users():
        return auth.list_users()

    @app.post("/api/users")
    def create_user(body: UserRequest):
        return auth.create_user(body.username, body.display_name, body.password)

    @app.patch("/api/users/{user_id}")
    def set_user_active(user_id: str, body: ActiveRequest):
        auth.set_active(user_id, body.active)
        return {"ok": True}

    @app.put("/api/users/{user_id}/password")
    def reset_password(user_id: str, body: PasswordRequest):
        auth.reset_password(user_id, body.password)
        return {"ok": True}

    @app.get("/api/projects/{project_id}/assignment")
    def project_assignment(project_id: str):
        service.load(project_id)
        return {"user_id": auth.assignment(project_id)}

    @app.put("/api/projects/{project_id}/assignment")
    def assign_project(project_id: str, body: AssignmentRequest):
        service.load(project_id)
        auth.assign(project_id, body.user_id)
        return {"user_id": body.user_id}

    @app.post("/api/projects/{project_id}/editing")
    def enter_editing(project_id: str, body: EditingSessionRequest, request: Request):
        service.load(project_id)
        return {"others": auth.enter_editing(project_id, request.state.user["id"], body.tab_id) if auth_required else []}

    @app.delete("/api/projects/{project_id}/editing")
    def leave_editing(project_id: str, body: EditingSessionRequest, request: Request):
        if auth_required:
            auth.leave_editing(project_id, request.state.user["id"], body.tab_id)
        return {"ok": True}

    @app.post("/api/browser/reserve")
    def reserve_browser():
        if not lifetime.reserve():
            raise HTTPException(503, "平台正在退出，请重新打开启动快捷方式。")
        return {"ok": True}

    @app.websocket("/api/browser/connection")
    async def browser_connection(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        expected = allowed_origins.copy() if configured_origin else {"http://" + websocket.headers.get("host", "")}
        # Vite's local development page uses a different local port.
        if not configured_origin and not on_idle:
            expected.update({"http://127.0.0.1:5173", "http://localhost:5173"})
        if origin not in expected or lifetime.stopping:
            await websocket.close(code=1008)
            return
        token = uuid.uuid4().hex
        await websocket.accept()
        if not lifetime.connect(token):
            await websocket.close(code=1001)
            return
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            lifetime.disconnect(token)

    @app.get("/api/projects")
    def projects(request: Request):
        result = service.projects()
        if not auth_required:
            return result
        allowed = auth.allowed_project_ids(request.state.user)
        return result if allowed is None else [project for project in result if project["id"] in allowed]

    @app.put("/api/local-reports")
    def put_local_report(body: LocalReportRequest, request: Request):
        if mock_mode:
            raise HTTPException(404, "S3 mock 不连接 NAS 项目同步。")
        return local_reports.put(request.state.user, body.snapshot, body.documents)

    @app.get("/api/local-reports")
    def list_local_reports(request: Request):
        if mock_mode:
            raise HTTPException(404, "S3 mock 不连接 NAS 项目同步。")
        return local_reports.list(request.state.user)

    @app.get("/api/local-reports/{project_id}/export")
    def export_local_report(project_id: str, request: Request):
        if mock_mode:
            raise HTTPException(404, "S3 mock 不连接 NAS 项目同步。")
        return Response(local_reports.documents(request.state.user, project_id), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{project_id}-timelines.zip"'})

    @app.post("/api/projects/open")
    def open_project(body: OpenRequest, request: Request):
        require_nas_source(body.path)
        path = Path(body.path.strip().strip('"')).expanduser()
        user = request.state.user if auth_required else None
        with service.lock:
            if path.is_file():
                if configured_origin:
                    raise HTTPException(422, "NAS 标注请领取完整采集目录，保证统一写回 timeline 文件夹。")
                project = service.open_files([str(path)], name=body.name, source_writeback=not configured_origin)
            else:
                allowed = auth.allowed_project_ids(user) if user and user["role"] == "annotator" else None
                if allowed is not None:
                    allowed.update(item["id"] for item in service.projects() if auth.assignment(item["id"]) is None)
                project = service.open_path(body.path, name=body.name, allowed_project_ids=allowed)
            if user and user["role"] == "annotator" and not auth.claim(project["id"], user["id"]):
                raise HTTPException(403, "该素材已由其他标注员领取。")
            return project

    @app.get("/api/sources/browse")
    def sources_browse(path: str | None = None, page: int = 0):
        if mock_mode:
            raise HTTPException(404, "S3 mock 不浏览 NAS。")
        if not configured_origin:
            raise HTTPException(404, "仅内网服务提供 NAS 目录浏览。")
        return browse_sources(nas_source_root, path, page)

    @app.post("/api/local-files/pick")
    def local_files(body: PickFilesRequest):
        if mock_mode:
            raise HTTPException(404, "S3 mock 不读取本机视频路径。")
        if configured_origin:
            raise HTTPException(503, "服务器模式不支持本机选择窗口，请填写服务器可访问的素材路径。")
        return {"paths": choose_local_paths(root, body.kind)}

    @app.post("/api/projects/files")
    def open_files(body: FilesRequest, request: Request):
        if mock_mode:
            raise HTTPException(404, "S3 mock 项目只接受测试视频上传。")
        if configured_origin:
            raise HTTPException(422, "NAS 标注请领取完整采集目录，保证统一写回 timeline 文件夹。")
        for value in body.paths:
            require_nas_source(value)
        user = request.state.user if auth_required else None
        with service.lock:
            project = service.open_files(body.paths, name=body.name, source_writeback=not configured_origin)
            if user and user["role"] == "annotator" and not auth.claim(project["id"], user["id"]):
                raise HTTPException(403, "该素材已由其他标注员领取。")
            return project

    def open_mock_folder(folder: Path, name: str, request: Request, key: str | None = None) -> dict:
        with service.lock:
            user = getattr(request.state, "user", None)
            allowed = auth.allowed_project_ids(user) if user and user["role"] == "annotator" else None
            if allowed is not None:
                allowed.update(item["id"] for item in service.projects() if auth.assignment(item["id"]) is None)
            project = service.open_path(str(folder), name=name or None, allowed_project_ids=allowed)
            stored = service.load(project["id"])
            stored["_mock_s3"] = True
            if key:
                stored["_mock_s3_key"] = key
            service.save(stored)
            if user and user["role"] == "annotator" and not auth.claim(stored["id"], user["id"]):
                raise HTTPException(403, "该项目已由其他标注员领取。")
            return service.public(stored)

    @app.get("/api/mock-s3/sources")
    def browse_mock_sources(prefix: str = ""):
        if not mock_catalog:
            raise HTTPException(404, "未配置模拟项目文件夹。")
        return mock_catalog.browse(prefix)

    @app.post("/api/mock-s3/projects/open")
    def open_mock_project(body: OpenRequest, request: Request):
        if not mock_catalog:
            raise HTTPException(404, "未配置模拟项目文件夹。")
        return open_mock_folder(mock_catalog.project_directory(body.path), body.name or "", request, mock_catalog.prefix)

    @app.post("/api/projects/upload")
    def legacy_upload():
        # Deliberately no multipart parser: never spool old-client video bytes.
        raise HTTPException(410, "平台已改为直接读取原视频，请刷新页面后选择本机文件或填写原目录路径。")

    @app.post("/api/projects/{project_id}/videos/open")
    def supplement_path(project_id: str, body: SupplementRequest):
        require_nas_source(body.path)
        return service.supplement_path(project_id, body.path, body.expected_revision)

    @app.post("/api/projects/{project_id}/videos/files")
    def supplement_files(project_id: str, body: SupplementFilesRequest):
        paths = []
        for value in body.paths:
            require_nas_source(value)
            if not value.strip():
                raise HTTPException(422, "请选择有效的视频文件路径。")
            try:
                path = Path(value.strip().strip('"')).expanduser().resolve(strict=True)
            except OSError:
                raise HTTPException(422, "无法访问所选视频，请检查原目录或 SD 卡连接。")
            if not path.is_file():
                raise HTTPException(422, "请选择视频文件，或通过目录路径补导入。")
            paths.append(path)
        return service.supplement(project_id, paths, body.expected_revision)

    @app.post("/api/projects/{project_id}/sources/relink")
    def relink_sources(project_id: str, body: SupplementRequest):
        require_nas_source(body.path)
        return service.relink_sources(project_id, body.path, body.expected_revision)

    @app.post("/api/projects/{project_id}/videos/upload")
    def legacy_supplement_upload(project_id: str):
        raise HTTPException(410, "平台已改为原目录读取，请刷新页面后使用补导入文件或目录，不再上传视频副本。")

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return service.public(service.load(project_id, allow_deleting=True))

    @app.patch("/api/projects/{project_id}/name")
    def rename_project(project_id: str, body: RenameProjectRequest):
        return service.rename_project(project_id, body.name, body.expected_revision)

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str, body: DeleteProjectRequest):
        return service.delete_project(project_id, body.expected_revision)

    @app.post("/api/projects/{project_id}/preview-cache/clear")
    def clear_local_previews(project_id: str, body: DeleteProjectRequest):
        return service.clear_local_previews(project_id, body.expected_revision)

    @app.put("/api/projects/{project_id}/draft")
    def draft(project_id: str, body: DraftRequest, request: Request):
        return service.update_draft(project_id, body.annotations, body.expected_revision, actor=request.state.user if auth_required else None)

    @app.post("/api/projects/{project_id}/custom-tracks")
    def add_custom_track(project_id: str, body: CustomTrackRequest):
        return service.add_custom_track(project_id, body.name, body.mode, body.labels, body.expected_revision)

    @app.put("/api/projects/{project_id}/fixed-tracks/{track_id}")
    def set_fixed_track(project_id: str, track_id: str, body: FixedTrackRequest):
        return service.set_fixed_track(project_id, track_id, body.enabled, body.expected_revision)

    @app.put("/api/projects/{project_id}/tracks/{track_id}/labels")
    def set_track_labels(project_id: str, track_id: str, body: TrackLabelsRequest, request: Request):
        return service.set_track_labels(project_id, track_id, body.labels, body.expected_revision,
                                        actor=request.state.user if auth_required else None)

    @app.delete("/api/projects/{project_id}/custom-tracks/{track_id}")
    def delete_custom_track(project_id: str, track_id: str, body: DeleteProjectRequest):
        return service.delete_custom_track(project_id, track_id, body.expected_revision)

    @app.get("/api/projects/{project_id}/history")
    def edit_history(project_id: str):
        return service.edit_history(project_id)

    @app.get("/api/projects/{project_id}/export")
    def export(project_id: str):
        return Response(service.export_zip(project_id), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="timelines-{project_id[:32]}.zip"'})

    @app.post("/api/projects/{project_id}/writeback")
    def writeback(project_id: str, body: WritebackRequest | None = None):
        if body is None:
            return service.writeback(project_id)
        return service.writeback(project_id, expected_revision=body.expected_revision)

    @app.post("/api/projects/{project_id}/submitted-cache/clear")
    def clear_submitted_cache(project_id: str, body: SubmittedCacheRequest):
        return service.clear_submitted_server_cache(project_id, body.expected_revision, body.save_id)

    @app.get("/api/projects/{project_id}/prepare")
    def preparation_status(project_id: str):
        return service.preparation_status(project_id)

    @app.post("/api/projects/{project_id}/prepare")
    def prepare_project(project_id: str, body: PreparationRequest | None = None):
        return service.prepare_project(project_id, retry_failed=body.retry_failed if body else False)

    @app.post("/api/projects/{project_id}/session/prepare")
    def prepare_session(project_id: str, body: PreparationRequest | None = None):
        return service.start_session(project_id, retry_failed=body.retry_failed if body else False,
                                     cache_generation=body.cache_generation if body else None)

    @app.get("/api/projects/{project_id}/session")
    def session_status(project_id: str):
        return service.sessions.status(project_id)

    @app.post("/api/projects/{project_id}/session/skip-failed")
    def skip_failed_videos(project_id: str, body: SkipFailedRequest):
        return service.skip_failed_videos(project_id, body.video_ids, body.expected_revision)

    @app.post("/api/projects/{project_id}/session/restore-skipped")
    def restore_skipped_videos(project_id: str, body: DeleteProjectRequest):
        return service.restore_skipped_videos(project_id, body.expected_revision)

    @app.get("/api/projects/{project_id}/session/manifest")
    def session_manifest(project_id: str, request: Request):
        manifest = service.sessions.manifest(project_id)
        if mock_s3:
            origin = f"{request.url.scheme}://{request.headers['host']}"
            return mock_s3.media_manifest(service, manifest, origin)
        return manifest

    @app.api_route("/mock-objects/{key:path}", methods=["GET", "HEAD"])
    def mock_object(key: str, expires: int, signature: str):
        if not mock_s3:
            raise HTTPException(404, "对象不存在。")
        path = mock_s3.authorized_path(key, expires, signature)
        media_type = "video/mp4" if path.suffix == ".mp4" else "image/jpeg" if path.suffix == ".jpg" else "application/json"
        return CancellableFileResponse(path, media_type=media_type,
                                       headers={"Cache-Control": "private, max-age=60", "Referrer-Policy": "no-referrer"})

    def session_file(project_id: str, version: str, video_id: str, kind: str, index: int | None = None):
        if mock_s3:
            raise HTTPException(404, "mock 播放素材只通过签名对象地址提供。")
        path = service.sessions.asset(project_id, kind, video_id, index, version=version)
        if path is None:
            raise HTTPException(409, "本机播放缓存尚未就绪，请重新打开素材准备面板。")
        return path

    @app.api_route("/api/session-media/{project_id}/{version}/{video_id}", methods=["GET", "HEAD"])
    def session_media(project_id: str, version: str, video_id: str, fast: bool = False):
        path = session_file(project_id, version, video_id, "fast" if fast else "normal")
        return CancellableFileResponse(path, media_type="video/mp4", content_disposition_type="inline",
                                       headers={"Cache-Control": "private, max-age=3600"})

    @app.api_route("/api/session-thumbnails/{project_id}/{version}/{video_id}", methods=["GET", "HEAD"])
    def session_thumbnail(project_id: str, version: str, video_id: str):
        return FileResponse(session_file(project_id, version, video_id, "thumbnail"), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.api_route("/api/session-storyboards/{project_id}/{version}/{video_id}/{index}", methods=["GET", "HEAD"])
    def session_storyboard(project_id: str, version: str, video_id: str, index: int):
        return FileResponse(session_file(project_id, version, video_id, "sheet", index), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/previews/{project_id}/{video_id}")
    def preview_status(project_id: str, video_id: str, fast: bool = False):
        return service.preview_status(project_id, video_id, fast=fast)

    @app.post("/api/previews/{project_id}/{video_id}")
    def prepare_preview(project_id: str, video_id: str, body: PreviewRequest | None = None, fast: bool = False):
        body = body or PreviewRequest()
        return service.prepare_preview(project_id, video_id, prefetch=body.prefetch, retry=body.retry, fast=fast)

    @app.get("/api/storyboards/{project_id}/{video_id}")
    def storyboard_status(project_id: str, video_id: str):
        return service.storyboard_status(project_id, video_id)

    @app.api_route("/api/storyboards/{project_id}/{video_id}/sheets/{index}", methods=["GET", "HEAD"])
    def storyboard_sheet(project_id: str, video_id: str, index: int):
        if mock_s3:
            raise HTTPException(404, "mock 图片只通过签名对象地址提供。")
        return FileResponse(service.storyboard_sheet(project_id, video_id, index), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.api_route("/api/media/{project_id}/{video_id}", methods=["GET", "HEAD"])
    def media(project_id: str, video_id: str, fast: bool = False):
        if mock_s3:
            raise HTTPException(404, "mock 播放素材只通过签名对象地址提供。")
        try:
            # Completed cache reads validate the source and ownership once.
            # Repeating a status lookup first adds another network round trip.
            path = service.media(project_id, video_id, fast=fast)
        except HTTPException as error:
            if error.status_code not in {404, 409}:
                raise
            status = service.preview_status(project_id, video_id, fast=fast)
            if status["state"] != "ready":
                return JSONResponse(status_code=202, content=status, headers={"Retry-After": "1", "Cache-Control": "no-store"})
            path = service.media(project_id, video_id, fast=fast)
        return CancellableFileResponse(path, media_type="video/mp4", content_disposition_type="inline")

    @app.api_route("/api/thumbnails/{project_id}/{video_id}", methods=["GET", "HEAD"])
    def thumbnail(project_id: str, video_id: str):
        if mock_s3:
            raise HTTPException(404, "mock 图片只通过签名对象地址提供。")
        return FileResponse(service.media(project_id, video_id, thumbnail=True), media_type="image/jpeg")

    @app.api_route("/api/{unknown_path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    def missing_api(unknown_path: str):
        raise HTTPException(404, "接口不存在。")

    dist = root / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @app.get("/")
        def not_built():
            return JSONResponse(status_code=503, content={"detail": "前端尚未构建，请先运行项目安装脚本。"})
    if public_origin:
        # Keep CORS outside authentication and host checks so a browser's
        # credential-free preflight reaches the exact-origin policy first.
        app.add_middleware(CORSMiddleware, allow_origins=[public_origin], allow_credentials=True,
                           allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Content-Type", "X-CSRF-Token", "Range"],
                           expose_headers=["Accept-Ranges", "Content-Range", "Content-Length"])
    return app
