"""Loopback-only upload window for macOS/Windows; independent of the NAS server."""
from __future__ import annotations

import argparse
import hmac
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .login_encryption import install_login_encryption
from .native_picker import choose_local_paths
from .public_setup import public_origin
from .r2_folder import FolderUploadManager
from .r2_upload import RemoteCaptureTarget
from .service import ProjectService


def create_app(*, api='https://api-annotate.reltydynamic.com:8443', root=None, port=8767, client_factory=None, service=None):
    public_origin(api, api=True)
    root = Path(root or Path(__file__).resolve().parents[1])
    if service is None:
        service = ProjectService(root / '.local/r2-uploader', enable_remote=False)
        service.desktop_cache.enabled = True
        tools = SimpleNamespace(root=root)
        service.tool = lambda name: ProjectService.tool(tools, name)
    origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
    sessions, lock = {}, threading.RLock()
    assets = Path(__file__).parent / 'upload_ui'

    @asynccontextmanager
    async def lifespan(app):
        yield
        for state in sessions.values():
            if state.get('client'):
                state['client'].close()
        service.sessions.close()
        service.previews.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])

    @app.middleware('http')
    async def local_only(request, call_next):
        if not request.client or request.client.host not in {'127.0.0.1', '::1', 'testclient'}:
            return JSONResponse({'detail': '上传助手仅允许本机访问。'}, status_code=403)
        if request.url.path.startswith('/api/'):
            with lock:
                state = sessions.get(request.cookies.get('datamark_upload', ''))
            if not state or (time.monotonic() - state['touched'] > 12 * 3600 and not state.get('busy')):
                return JSONResponse({'detail': '上传窗口已过期，请重新打开。'}, status_code=401)
            if not hmac.compare_digest(request.headers.get('x-upload-token', ''), state['csrf']):
                return JSONResponse({'detail': '上传窗口验证失败，请刷新。'}, status_code=403)
            if request.headers.get('origin') and request.headers['origin'] not in origins:
                return JSONResponse({'detail': '请在本机上传窗口操作。'}, status_code=403)
            if request.method != 'GET' and request.headers.get('origin') not in origins:
                return JSONResponse({'detail': '请在本机上传窗口操作。'}, status_code=403)
            state['touched'] = time.monotonic()
            request.state.upload = state
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.get('/')
    def page(request: Request):
        sid = request.cookies.get('datamark_upload', '')
        with lock:
            for key, state in list(sessions.items()):
                if not state.get('busy') and time.monotonic() - state['touched'] > 12 * 3600:
                    if state.get('client'):
                        state['client'].close()
                    del sessions[key]
            if sid not in sessions:
                if len(sessions) >= 16:
                    raise HTTPException(429, '本机上传窗口过多，请关闭旧窗口并重启助手。')
                sid = secrets.token_urlsafe(32)
                sessions[sid] = {'csrf': secrets.token_urlsafe(32), 'touched': time.monotonic(), 'busy': False}
            state = sessions[sid]
        response = HTMLResponse((assets / 'index.html').read_text().replace('__UPLOAD_TOKEN__', state['csrf']))
        response.set_cookie('datamark_upload', sid, httponly=True, samesite='strict', max_age=12*3600)
        return response

    @app.get('/upload.js')
    def javascript():
        return FileResponse(assets / 'upload.js', media_type='application/javascript')

    @app.get('/upload.css')
    def stylesheet():
        return FileResponse(assets / 'upload.css', media_type='text/css')

    @app.get('/response.mjs')
    def response_reader():
        return FileResponse(assets / 'response.mjs', media_type='application/javascript')

    def idle(state):
        if state.get('busy'):
            raise HTTPException(409, '正在压缩或上传，请等待本次任务完成。')

    def target(state):
        if not state.get('client'):
            raise HTTPException(401, '请先登录公网上传账号。')
        remote = RemoteCaptureTarget(state['client'], service)
        remote.pipeline_enabled = state.get('pipeline_enabled',False)
        return remote

    def authorized(state):
        remote = target(state)
        try:
            user = remote.request('GET', '/api/auth/me')
        except httpx.HTTPError:
            raise HTTPException(503, '公网后端连接失败，请检查网络。') from None
        except RuntimeError:
            raise HTTPException(401, '公网登录已过期，请重新登录。') from None
        if not user.get('can_upload'):
            raise HTTPException(403, '管理员尚未开放此账号的上传权限。')
        state['user'] = user
        return user

    def login(credentials, request):
        state = request.state.upload
        with lock:
            idle(state)
            client = None
            try:
                client = (client_factory() if client_factory else httpx.Client(base_url=api, timeout=30, trust_env=False))
                health = client.get('/api/health').json()
                if health.get('mode') != 's3' or health.get('ready') is not True:
                    raise HTTPException(503, '公网 S3 服务尚未就绪。')
                if 's3-folder-upload' not in health.get('capabilities', []):
                    raise HTTPException(503, '公网后端尚未更新到文件夹上传版本，请先更新公网服务。')
                # Browser credentials arrive in a single-use encrypted envelope;
                # plaintext only exists in this process and the existing HTTPS API.
                result = client.post('/api/auth/login', json=credentials, headers={'Origin': api})
                if result.status_code != 200:
                    raise HTTPException(401, '账号或密码错误，或暂时被限制登录。')
                value = result.json()
                if not value['user'].get('can_upload'):
                    raise HTTPException(403, '管理员尚未开放此账号的上传权限。')
                client.headers.update({'Origin': api, 'X-CSRF-Token': value['csrf']})
            except Exception as error:
                if client is not None:
                    client.close()
                if isinstance(error, HTTPException):
                    raise
                if isinstance(error, ImportError):
                    raise HTTPException(503, '上传助手缺少网络依赖，请在项目虚拟环境安装 requirements.lock.txt 后重启助手。') from None
                raise HTTPException(503, '无法连接公网服务，请检查网络。') from None
            if state.get('client'):
                state['client'].close()
            state.update(client=client, user=value['user'], selection=None, manager=None, job=None,
                         pipeline_enabled='s3-upload-pipeline-v1' in health.get('capabilities',[]))
        return {'user': value['user']}

    install_login_encryption(app, login)

    @app.post('/api/select')
    def select(body: dict, request: Request):
        state = request.state.upload
        with lock:
            idle(state)
            user = authorized(state)
            folders = choose_local_paths(root, 'directory')
            if not folders:
                return {'cancelled': True}
            remote = target(state)
            manager = FolderUploadManager(remote, service.local / 'folder-staging')
            selection = manager.select_folder(user['id'], folders[0])
            remote.attachments = selection['attachments']
            state.update(selection=selection, manager=manager, job=None)
        return {'name': selection['name'], 'capture_date': selection['capture_date'],
                'videos': [item['name'] for item in selection['videos']],
                'attachments': [item['output'] for item in selection['attachments']],
                'ignored': selection['ignored']}

    def work(state, job, user):
        manager = state['manager']
        def progress(detail):
            with manager.lock:
                manager.jobs[job['id']].update(detail=detail)
        manager.store.progress = progress
        try:
            manager.finish(job['id'], user['id'], service, lambda ident: authorized(state)['id'] == ident)
            with manager.lock:
                manager.jobs[job['id']]['detail'] = '全部文件已核验并发布到 S3；视频缓存已保留到桌面。'
        except Exception as error:
            # Do not log HTTP exceptions: requests may contain signed URLs.
            detail = error.detail if isinstance(error, HTTPException) and isinstance(error.detail, str) else '上传未完成，请检查网络、账号权限或采集设备，重新选择目录可重试。'
            with manager.lock:
                manager.jobs[job['id']].update(state='error', detail=detail)
        finally:
            with lock:
                state['busy'] = False

    @app.post('/api/start')
    def start(body: dict, request: Request):
        state = request.state.upload
        with lock:
            idle(state)
            if any(value.get('busy') for value in sessions.values()):
                raise HTTPException(409, '本机已有上传任务，请等待完成后再开始。')
            user = authorized(state)
            if not state.get('selection'):
                raise HTTPException(422, '请先选择采集文件夹。')
            if any(not isinstance(body.get(key, ''), str) for key in ('collector', 'uploader', 'note')):
                raise HTTPException(422, '采集人、上传人和备注须为文字。')
            manager = state['manager']
            job = manager.start(user, body.get('collector', ''), body.get('uploader'), body.get('note'), state['selection']['selection_id'], prediction=body)
            state.update(job=job['id'], busy=True)
            threading.Thread(target=work, args=(state, job, user), daemon=True).start()
        return job

    @app.get('/api/status')
    def status(request: Request):
        state = request.state.upload
        result = {'api': api, 'busy': state['busy'], 'user': state.get('user')}
        if state.get('job'):
            result['job'] = state['manager'].status(state['job'], state['user']['id'])
        return result

    @app.post('/api/logout')
    def logout(body: dict, request: Request):
        state = request.state.upload
        with lock:
            idle(state)
            if state.get('client'):
                try:
                    target(state).request('POST', '/api/auth/logout')
                except (RuntimeError, httpx.HTTPError, HTTPException):
                    pass
                state['client'].close()
            for key in ('client', 'user', 'selection', 'manager', 'job'):
                state.pop(key, None)
        return {'ok': True}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='https://api-annotate.reltydynamic.com:8443')
    args = parser.parse_args()
    os.environ['PYTHONNOUSERSITE'] = '1'
    import uvicorn
    print('本机上传窗口：http://127.0.0.1:8767（请在浏览器打开；不影响 8765 NAS 本机服务）')
    uvicorn.run(create_app(api=args.api), host='127.0.0.1', port=8767, access_log=False)


if __name__ == '__main__':
    main()
