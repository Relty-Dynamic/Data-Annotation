"""Private account bridge to the existing NAS account database; no media APIs."""
from __future__ import annotations

import os
import hashlib
import hmac
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import AuthStore


class LoginBody(BaseModel):
    username: str = Field(strict=True, max_length=254)
    password: str = Field(strict=True, max_length=1024)
    client: str = Field(strict=True, max_length=200)


class SessionBody(BaseModel):
    token: str | None = Field(default=None, max_length=200)


class AdminAccountBody(SessionBody):
    csrf: str = Field(default='', max_length=200)
    action: str = Field(max_length=30)
    body: dict = Field(default_factory=dict)


def create_app(root: Path | None = None) -> FastAPI:
    root = root or Path(os.environ["DATAMARK_SHARED_ACCOUNT_ROOT"])
    if not (root / ".local/auth.sqlite3").is_file():
        raise RuntimeError("Existing shared account database is required; no new account database will be created")
    auth = AuthStore(root)
    if not auth.has_admin():
        raise RuntimeError("Shared account database has no active administrator")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["accounts", "127.0.0.1", "localhost"])
    app.state.auth = auth

    @app.get("/v1/health")
    def health():
        return {"accounts_ready": auth.has_admin()}

    @app.post("/v1/login")
    def login(body: LoginBody):
        user, token, csrf = auth.login(body.username, body.password, "public|" + body.client)
        return {"user": user, "token": token, "csrf": csrf}

    @app.post("/v1/session")
    def session(body: SessionBody):
        saved = auth.session(body.token)
        if not saved:
            raise HTTPException(401, "请先登录。")
        user, csrf_hash = saved
        return {"user": user, "csrf_hash": csrf_hash}

    @app.post("/v1/logout")
    def logout(body: SessionBody):
        auth.logout(body.token)
        return {"ok": True}

    @app.post('/v1/users')
    def directory(body: SessionBody):
        if not auth.session(body.token):
            raise HTTPException(401, '请先登录。')
        # Fixed private bridge. The task host additionally checks manager roles;
        # no passwords, email addresses or session records leave this endpoint.
        return {'users': [{key: item.get(key) for key in ('id','display_name','role','active','can_upload')}
                          for item in auth.list_users(include_deleted=True)]}

    @app.post('/v1/admin/accounts')
    def admin_accounts(body: AdminAccountBody):
        saved = auth.session(body.token)
        if not saved:
            raise HTTPException(401, '请先登录。')
        user, csrf_hash = saved
        if user['role'] != 'admin':
            raise HTTPException(403, '只有超级管理员可管理账号。')
        if not body.csrf or not hmac.compare_digest(hashlib.sha256(body.csrf.encode()).hexdigest(), csrf_hash):
            raise HTTPException(403, '页面安全令牌已失效，请刷新页面。')
        from .tasks.accounts import administer
        return {'result': administer(auth, user, body.action, body.body)}

    @app.post('/v1/axis-library')
    def axis_library(body: SessionBody):
        saved = auth.session(body.token)
        if not saved:
            raise HTTPException(401, '请先登录。')
        from .track_library import TrackLibrary
        from .service import DEFAULT_TRACK_LABELS, AXIS_NAMES
        with auth.connection() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_library'").fetchone():
                return {'axes': [{'id': key, 'name': AXIS_NAMES[key], 'mode':'state', 'labels': labels}
                                 for key, labels in DEFAULT_TRACK_LABELS.items() if key in ('scene','posture','category')]}
            rows = con.execute("SELECT * FROM track_library WHERE scope IN (?,?) ORDER BY CASE scope WHEN '*' THEN 0 ELSE 1 END,rowid", ('*', saved[0]['id'])).fetchall()
            return {'axes': [TrackLibrary.track(row) for row in rows]}

    return app


class AccountClient:
    """Fixed internal endpoint; public requests cannot select a proxy target."""

    def __init__(self, client=None):
        self.client = client or httpx.Client(base_url="http://accounts:8766", timeout=10, trust_env=False)

    def request(self, method: str, path: str, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.RequestError as error:
            raise HTTPException(503, "共享账号服务暂时不可用，请稍后重试。") from error
        if response.status_code != 200:
            detail = "共享账号服务暂时不可用，请稍后重试。"
            if response.status_code in {401, 403, 409, 422, 429}:
                detail = response.json().get("detail", detail)
            raise HTTPException(response.status_code if response.status_code in {401, 403, 409, 422, 429} else 503, detail)
        return response.json()

    def close(self):
        self.client.close()
