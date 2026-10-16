"""Authenticated, owner-scoped proxy to a separately deployed IMU worker."""
from __future__ import annotations

import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

MAX_JSON = 4 * 1024**2
MAX_INPUT = 1024**3


class Gateway:
    def __init__(self, url='', token_file=None, *, transport=None):
        self.url = url.rstrip('/')
        self.token_file, self.transport = token_file, transport
        if self.url:
            parsed = urlsplit(self.url)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                    or parsed.password or parsed.path or parsed.query or parsed.fragment or not token_file):
                raise ValueError('Configure a private prediction worker origin and protected token file')

    async def forward(self, user, method, path, *, content=None, length=None):
        if not user.get('can_upload'):
            raise HTTPException(403, '管理员尚未开放上传权限。')
        if not self.url:
            if method == 'GET' and path == 'capabilities':
                return Response(b'{"enabled":false}', media_type='application/json')
            raise HTTPException(503, '姿势预标注服务尚未启用。')
        try:
            token = Path(self.token_file).read_text().strip()
            if not 32 <= len(token) <= 128 or not token.isascii() or any(c.isspace() for c in token):
                raise OSError('Invalid worker credential')
            headers = {'Authorization': 'Bearer ' + token, 'X-Prediction-Owner': user['id']}
            if length is not None:
                headers['Content-Length'] = str(length)
            limit = 20 * 1024**2 if path.endswith('/prediction') else MAX_JSON
            async with httpx.AsyncClient(timeout=180, follow_redirects=False, trust_env=False,
                                         transport=self.transport) as client:
                async with client.stream(method, self.url + '/v1/' + path, headers=headers, content=content) as result:
                    payload = bytearray()
                    async for chunk in result.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > limit:
                            raise HTTPException(502, '预标注服务返回结果过大。')
                    if result.status_code >= 500 or result.status_code in (401, 403) or 300 <= result.status_code < 400:
                        raise HTTPException(503, '预标注服务暂时不可用，请按原任务重试。')
                    if result.status_code >= 400:
                        # The private worker supplies bounded, public-safe errors.
                        try:
                            import json
                            detail = json.loads(payload)['detail']
                        except (ValueError, KeyError):
                            detail = '预标注请求未通过校验。'
                        raise HTTPException(result.status_code, detail)
                    if path == 'capabilities':
                        import json
                        payload = json.dumps({**json.loads(payload), 'enabled': True}).encode()
                    return Response(bytes(payload), media_type='application/zip' if path == 'client.zip' else 'application/json',
                                    headers={'Cache-Control': 'no-store'})
        except (OSError, httpx.HTTPError):
            raise HTTPException(503, '预标注服务连接失败，请稍后重试；已有压缩和上传结果保留。') from None


def install(app, router, actor, bridge):
    gateway = Gateway(os.getenv('DATAMARK_PREDICTION_URL', ''), os.getenv('DATAMARK_PREDICTION_TOKEN_FILE'))

    async def proxy(request, user, path):
        if not re.fullmatch(r'(capabilities|client\.zip|jobs(?:/[0-9a-f]{64}(?:/(start|motion|prediction|inputs/[0-9]{1,4}))?)?)', path):
            raise HTTPException(404, '预标注接口不存在。')
        content, length = None, None
        if request.method in ('POST', 'PUT'):
            raw = request.headers.get('content-length', '')
            if not raw.isdecimal() or request.headers.get('transfer-encoding'):
                raise HTTPException(411, '需要明确传输大小。')
            length = int(raw)
            if length > (MAX_INPUT if '/inputs/' in path else MAX_JSON):
                raise HTTPException(413, '预标注请求过大。')
            async def bounded():
                received = 0
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > length:
                        raise HTTPException(422, '传输大小与声明不一致。')
                    yield chunk
                if received != length:
                    raise HTTPException(422, '传输不完整，可重试。')
            content = bounded()
        return await gateway.forward(user, request.method, path, content=content, length=length)

    @router.api_route('/prediction/{path:path}', methods=['GET', 'POST', 'PUT'])
    async def browser(request: Request, path: str):
        return await proxy(request, actor(request), path)

    helper = APIRouter()

    @helper.api_route('/api/r2/helper/prediction/{path:path}', methods=['GET', 'POST', 'PUT'])
    async def uploader(request: Request, path: str):
        return await proxy(request, bridge.actor(bridge.agent(request)), path)

    app.include_router(helper)
    app.state.prediction_gateway = gateway
