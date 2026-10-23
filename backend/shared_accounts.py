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


class PasswordBody(SessionBody):
    csrf: str = Field(default='', max_length=200)
    current_password: str = Field(strict=True, max_length=1024)
    password: str = Field(strict=True, min_length=8, max_length=1024)


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
        return {'users': [{key: item.get(key) for key in ('id','display_name','role','active','can_upload','role_model','task_access')}
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
        if body.action == 'editor-create':
            fields = body.body
            if any(not isinstance(fields.get(key), str) for key in ('username', 'display_name', 'password')):
                raise HTTPException(422, '请填写账号、姓名和初始密码。')
            if fields.get('email') is not None and not isinstance(fields['email'], str):
                raise HTTPException(422, '邮箱无效。')
            return {'result': auth.create_user(fields['username'], fields['display_name'], fields['password'], email=fields.get('email'))}
        from .tasks.accounts import administer
        return {'result': administer(auth, user, body.action, body.body)}

    @app.post('/v1/password')
    def password(body: PasswordBody):
        saved = auth.session(body.token)
        if not saved:
            raise HTTPException(401, '请先登录。')
        user, csrf_hash = saved
        if not body.csrf or not hmac.compare_digest(hashlib.sha256(body.csrf.encode()).hexdigest(), csrf_hash):
            raise HTTPException(403, '页面安全令牌已失效，请刷新页面。')
        auth.change_password(user['id'], body.current_password, body.password)
        return {'ok': True}

    @app.post('/v1/library-edit')
    def library_edit(body: dict):
        saved = auth.session(body.get('token'))
        if not saved:
            raise HTTPException(401, '请先登录。')
        user, csrf_hash = saved
        csrf = body.get('csrf')
        if not isinstance(csrf, str) or not hmac.compare_digest(hashlib.sha256(csrf.encode()).hexdigest(), csrf_hash):
            raise HTTPException(403, '页面安全令牌已失效，请刷新页面。')
        from .track_library import TrackLibrary
        library = TrackLibrary(auth)
        data = body.get('body', {})
        if not isinstance(data, dict):
            raise HTTPException(422, '时间轴定义无效。')
        default = body.get('default', False) if body.get('method') == 'DELETE' else data.get('is_default', False)
        if type(default) is not bool:
            raise HTTPException(422, '时间轴类型无效。')
        if (default or body.get('toggle_default')) and user['role'] != 'admin':
            raise HTTPException(403, '默认时间轴只能由管理员维护。')
        track_id = body.get('track_id') or data.get('id')
        if body.get('toggle_default'):
            result = library.set_default(user['id'], track_id, default)
        elif body.get('method') == 'DELETE':
            if data.get('confirmed') is not True:
                raise HTTPException(422, '请确认删除时间轴。')
            library.delete(user['id'], track_id, default=default)
            result = {'ok': True}
        elif body.get('method') in ('POST', 'PUT'):
            if data.get('id') != track_id:
                raise HTTPException(422, '时间轴 JSON 名不能修改。')
            result = library.save(user['id'], track_id, data.get('name'), data.get('mode', 'state'),
                                  data.get('labels'), default=default, create=body['method'] == 'POST')
        else:
            raise HTTPException(422, '时间轴操作无效。')
        return {'result': result}

    @app.post('/v1/axis-library')
    def axis_library(body: SessionBody):
        saved = auth.session(body.token)
        if not saved:
            raise HTTPException(401, '请先登录。')
        from .track_library import TrackLibrary
        from .service import DEFAULT_TRACK_LABELS, AXIS_NAMES
        with auth.connection() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_library'").fetchone():
                return {'axes': [{'id': key, 'name': AXIS_NAMES[key], 'mode':'state', 'labels': labels, 'is_default': True}
                                 for key, labels in DEFAULT_TRACK_LABELS.items() if key in ('scene','posture','category')]}
        return {'axes': TrackLibrary(auth).list(saved[0]['id'])}

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
