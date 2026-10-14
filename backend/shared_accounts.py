"""Private account bridge to the existing NAS account database; no media APIs."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import AuthStore


class LoginBody(BaseModel):
    username: str = Field(strict=True, max_length=120)
    password: str = Field(strict=True, max_length=1024)
    client: str = Field(strict=True, max_length=200)


class SessionBody(BaseModel):
    token: str | None = Field(default=None, max_length=200)


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
            if response.status_code in {401, 403, 429}:
                detail = response.json().get("detail", detail)
            raise HTTPException(response.status_code if response.status_code in {401, 403, 429} else 503, detail)
        return response.json()

    def close(self):
        self.client.close()
