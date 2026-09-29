from __future__ import annotations

import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from .service import ProjectService
from .browser_lifetime import BrowserLifetime
from .native_picker import choose_local_paths
from .media_response import CancellableFileResponse


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


def create_app(root: Path | None = None, on_idle=None) -> FastAPI:
    root = root or Path(os.getenv("DATAMARK_ROOT", str(Path(__file__).resolve().parents[1])))
    service = ProjectService(root)
    lifetime = BrowserLifetime(on_idle)
    @asynccontextmanager
    async def lifespan(app):
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
    app.state.browser_lifetime = lifetime
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        # Block cross-site drive-by mutations while allowing the same localhost UI
        # and CLI tools. Bind uvicorn to 127.0.0.1 in the project launcher.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site == "cross-site":
                return JSONResponse(status_code=403, content={"detail": "仅允许本机标注页面修改数据。"})
            if origin:
                try:
                    parsed = urlsplit(origin)
                    valid = parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                except ValueError:
                    valid = False
                if not valid:
                    return JSONResponse(status_code=403, content={"detail": "仅允许本机标注页面修改数据。"})
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
        if request.url.path.startswith(("/api/projects", "/api/previews")) or (request.url.path.startswith("/api/storyboards/") and "/sheets/" not in request.url.path):
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
        return {"status": "ok", "application": "datamark", "browser_lifetime": bool(on_idle), "stopping": lifetime.stopping, "ffmpeg": bool(service.tool("ffmpeg")), "ffprobe": bool(service.tool("ffprobe")), "remote_processing": service.remote is not None, "capabilities": ["source-local-cache", "native-file-picker", "supplement-import", "compact-local-playback", "direct-compact-preparation", "parallel-compact-preparation", "four-axis-annotations", "project-naming", "remote-nas-processing"]}

    @app.post("/api/browser/reserve")
    def reserve_browser():
        if not lifetime.reserve():
            raise HTTPException(503, "平台正在退出，请重新打开启动快捷方式。")
        return {"ok": True}

    @app.websocket("/api/browser/connection")
    async def browser_connection(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        expected = {"http://" + websocket.headers.get("host", "")}
        # Vite's local development page uses a different local port.
        if not on_idle:
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
    def projects():
        return service.projects()

    @app.post("/api/projects/open")
    def open_project(body: OpenRequest):
        path = Path(body.path.strip().strip('"')).expanduser()
        return service.open_files([str(path)], name=body.name) if path.is_file() else service.open_path(body.path, name=body.name)

    @app.post("/api/local-files/pick")
    def local_files(body: PickFilesRequest):
        return {"paths": choose_local_paths(root, body.kind)}

    @app.post("/api/projects/files")
    def open_files(body: FilesRequest):
        return service.open_files(body.paths, name=body.name)

    @app.post("/api/projects/upload")
    def legacy_upload():
        # Deliberately no multipart parser: never spool old-client video bytes.
        raise HTTPException(410, "平台已改为直接读取原视频，请刷新页面后选择本机文件或填写原目录路径。")

    @app.post("/api/projects/{project_id}/videos/open")
    def supplement_path(project_id: str, body: SupplementRequest):
        return service.supplement_path(project_id, body.path, body.expected_revision)

    @app.post("/api/projects/{project_id}/videos/files")
    def supplement_files(project_id: str, body: SupplementFilesRequest):
        paths = []
        for value in body.paths:
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
    def draft(project_id: str, body: DraftRequest):
        return service.update_draft(project_id, body.annotations, body.expected_revision)

    @app.get("/api/projects/{project_id}/export")
    def export(project_id: str):
        return Response(service.export_zip(project_id), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="timelines-{project_id[:32]}.zip"'})

    @app.post("/api/projects/{project_id}/writeback")
    def writeback(project_id: str):
        return service.writeback(project_id)

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
    def session_manifest(project_id: str):
        return service.sessions.manifest(project_id)

    def session_file(project_id: str, version: str, video_id: str, kind: str, index: int | None = None):
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
        return FileResponse(service.storyboard_sheet(project_id, video_id, index), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.api_route("/api/media/{project_id}/{video_id}", methods=["GET", "HEAD"])
    def media(project_id: str, video_id: str, fast: bool = False):
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
    return app

