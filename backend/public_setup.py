"""Public shared login and optional private R2 business APIs."""
from __future__ import annotations

import os
import hashlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .shared_accounts import AccountClient

MESSAGE = "公网后端已启动，S3 和公网账号尚未配置，暂不能登录、上传或标注。"
STORAGE_MESSAGE = "已连接共享账号；S3 尚未配置，暂不能上传或标注。"


class LoginRequest(BaseModel):
    username: str = Field(strict=True, max_length=254)
    password: str = Field(strict=True, max_length=1024)


def public_origin(value: str, *, api: bool = False) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.port not in ({None, 8443} if api else {None})
            or value != "https://" + parsed.hostname + (":8443" if parsed.port == 8443 else "") or "." not in parsed.hostname
            or any(not label or not label[0].isalnum() or not label[-1].isalnum()
                   or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
                   for label in parsed.hostname.split("."))):
        raise ValueError("Public setup requires a plain HTTPS frontend origin and an API origin on 443 or 8443")
    return value


def create_app(*, accounts: AccountClient | None = None) -> FastAPI:
    frontend = public_origin(os.getenv("DATAMARK_PUBLIC_ORIGIN", ""))
    api = public_origin(os.getenv("DATAMARK_PUBLIC_API_ORIGIN", ""), api=True)
    if urlsplit(frontend).hostname == urlsplit(api).hostname:
        raise ValueError("Public frontend and API must use separate hostnames")
    shared = os.getenv("DATAMARK_SHARED_ACCOUNTS", "0") == "1"
    accounts = accounts or (AccountClient() if shared else None)
    from .tasks.routes import enabled as tasks_enabled, install as install_tasks
    task_workspaces = None
    r2_service = r2_media = None
    r2_config = os.getenv('DATAMARK_R2_CONFIG', '').strip()
    if r2_config:
        state = Path(os.getenv('DATAMARK_R2_STATE', ''))
        if not state.is_absolute() or not accounts:
            raise ValueError('R2 requires a private persistent state directory and shared accounts')
        from .r2_store import R2Store
        r2_provider = R2Store(Path(r2_config))
        r2_provider.health()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if accounts:
                accounts.close()
            if task_workspaces:
                task_workspaces.close()
            if r2_service:
                r2_service.sessions.close()
                r2_service.previews.close()
    app = FastAPI(title="DataMark public setup", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(api).hostname, "127.0.0.1", "localhost"])

    @app.middleware("http")
    async def headers(request: Request, call_next):
        if request.url.path.startswith('/api/tasks/') and not tasks_enabled():
            return JSONResponse(status_code=404, content={'detail': '任务管理未启用。'})
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") not in {frontend, api}:
            response = JSONResponse(status_code=403, content={"detail": "仅允许已配置的标注页面访问。"})
        else:
            response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
        return response

    @app.get("/api/health")
    def health():
        ready = False
        if accounts:
            try:
                ready = accounts.request("GET", "/v1/health")["accounts_ready"] is True
            except HTTPException:
                pass
        if r2_service:
            try:
                storage_ready = r2_provider.health() is True
            except HTTPException:
                storage_ready = False
            return {'status': 'ok' if ready and storage_ready else 'degraded', 'application': 'datamark', 'mode': 's3', 'storage_backend': 's3',
                    'ready': ready and storage_ready, 'storage_ready': storage_ready, 'accounts_ready': ready, 'browser_lifetime': False,
                    'capabilities': ['source-local-cache', 'compact-local-playback', 'four-axis-annotations',
                        'subaxis-annotations', 'project-naming', 'account-login', 'shared-accounts', 'account-track-library',
                        'desktop-playback-cache', 's3-catalog'] + (['task-management'] if task_workspaces else [])}
        return {"status": "ok", "application": "datamark", "mode": "public-setup",
                "ready": False, "storage_backend": "unconfigured", "storage_ready": False,
                "accounts_ready": ready, "browser_lifetime": False,
                "capabilities": ["public-api-setup", "browser-connection"] + (["account-login", "shared-accounts"] if ready else []) + (['task-management'] if task_workspaces else []),
                "detail": STORAGE_MESSAGE if ready else MESSAGE}

    def account_service():
        if not accounts:
            raise HTTPException(503, MESSAGE)
        return accounts

    def session(request: Request):
        saved = account_service().request("POST", "/v1/session", json={"token": request.cookies.get("datamark_session")})
        csrf = request.cookies.get("datamark_csrf", "")
        if not csrf or not hmac.compare_digest(hashlib.sha256(csrf.encode()).hexdigest(), saved["csrf_hash"]):
            raise HTTPException(401, "登录状态不完整，请重新登录。")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
            raise HTTPException(403, "页面安全令牌已失效，请刷新页面。")
        return {**saved["user"], "csrf": csrf}

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request):
        saved = account_service().request("POST", "/v1/login", json={**body.model_dump(), "client": request.client.host if request.client else "unknown"})
        response = JSONResponse({"user": saved["user"], "csrf": saved["csrf"]})
        response.set_cookie("datamark_session", saved["token"], httponly=True, secure=True, samesite="strict", max_age=12 * 3600, path="/")
        response.set_cookie("datamark_csrf", saved["csrf"], httponly=False, secure=True, samesite="strict", max_age=12 * 3600, path="/")
        return response

    @app.get("/api/auth/me")
    def me(request: Request):
        if not accounts:
            return unavailable("auth/me")
        return session(request)

    @app.post("/api/auth/logout")
    def logout(request: Request):
        session(request)
        account_service().request("POST", "/v1/logout", json={"token": request.cookies.get("datamark_session")})
        response = JSONResponse({"ok": True})
        response.delete_cookie("datamark_session", path="/", secure=True, samesite="strict")
        response.delete_cookie("datamark_csrf", path="/", secure=True, samesite="strict")
        return response

    @app.websocket("/api/browser/connection")
    async def browser_connection(websocket: WebSocket):
        if websocket.headers.get("origin") not in {frontend, api}:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass

    if r2_config:
        from .r2_routes import install as install_r2
        r2_service, r2_media = install_r2(app, session, accounts, r2_provider, state)

    if tasks_enabled():
        if not accounts:
            raise ValueError('Tasks require shared accounts on the public host')
        from .tasks.accounts import RemoteAccounts
        from .tasks.storage import ObjectArtifacts
        task_workspaces = install_tasks(app, session, lambda request: account_service().request(
            'POST', '/v1/users', json={'token': request.cookies.get('datamark_session')})['users'],
            axis_library=lambda user, request: account_service().request(
                'POST', '/v1/axis-library', json={'token': request.cookies.get('datamark_session')})['axes'],
            accounts=RemoteAccounts(accounts), media=r2_media,
            source_tools=r2_media.source_tools if r2_media else None,
            artifact_store=ObjectArtifacts(r2_provider) if r2_media else None)

    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    def unavailable(path: str):
        return JSONResponse(status_code=503, content={"detail": STORAGE_MESSAGE if accounts else MESSAGE, "code": "public_setup_required"})

    app.add_middleware(CORSMiddleware, allow_origins=[frontend], allow_credentials=True,
                       allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Content-Type", "X-CSRF-Token", "Range"])
    return app
