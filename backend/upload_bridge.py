"""Scoped, short-lived upload-agent relay. No browser-to-localhost requests."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import uuid

from fastapi import HTTPException, Request


class UploadBridge:
    def __init__(self, accounts, clock=time.monotonic, *, current_version=lambda:None):
        self.accounts, self.clock = accounts, clock
        self.current_version = current_version
        self.lock = threading.RLock()
        self.agents = {}

    def register(self, body, client='local'):
        platform = body.get('platform')
        if platform not in {'darwin', 'win32'} or body.get('protocol') != 1:
            raise HTTPException(422, '请安装当前版本的 macOS／Windows 上传组件。')
        with self.lock:
            self.agents = {k: v for k, v in self.agents.items() if v['expires'] > self.clock()}
            if len(self.agents) >= 128:
                raise HTTPException(429, '上传组件连接过多，请稍后重试。')
            if sum(value.get('client')==client and value['owner'] is None for value in self.agents.values()) >= 16:
                raise HTTPException(429, '此网络待关联组件过多，请稍后重试。')
            ident, token, claim = uuid.uuid4().hex, secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.agents[ident] = {'id': ident, 'token_hash': self.digest(token), 'claim_hash': self.digest(claim),
                'platform': platform, 'component_version':body.get('component_version'), 'expires': self.clock()+300, 'seen': self.clock(), 'owner': None,
                'session': None, 'command': None, 'report': {}, 'delivered': False, 'client':client}
            return {'id': ident, 'token': token, 'claim': claim, 'expires_in': 300, 'protocol': 1}

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def agent(self, request):
        raw = request.headers.get('authorization', '')
        if not raw.startswith('Bearer ') or len(raw) > 200:
            raise HTTPException(401, '上传组件连接凭据已失效，请重新启动组件。')
        ident = request.headers.get('x-upload-agent', '')
        with self.lock:
            value = self.agents.get(ident)
            if not value or value['expires'] <= self.clock() or not hmac.compare_digest(self.digest(raw[7:]), value['token_hash']):
                raise HTTPException(401, '上传组件连接凭据已失效，请重新启动组件。')
            return value

    def pair(self, user, body, session_token):
        if not user.get('can_upload'):
            raise HTTPException(403, '管理员尚未开放此账号的上传权限。')
        claim = body.get('claim')
        if not isinstance(claim, str) or not 20 <= len(claim) <= 100:
            raise HTTPException(422, '本机组件关联信息无效。')
        with self.lock:
            value = self.agents.get(body.get('id'))
            if (not value or value['expires'] <= self.clock() or value['owner']
                    or not value['claim_hash'] or not hmac.compare_digest(self.digest(claim), value['claim_hash'])):
                raise HTTPException(409, '关联已过期或已使用，请重新启动本机组件。')
            value.update(owner=user['id'], session=session_token, claim_hash=None, expires=self.clock()+12*3600)
            return self.public(value)

    def actor(self, value):
        if not value.get('session'):
            raise HTTPException(401, '请先在原 DataMark 页面关联上传组件。')
        # Keep the parent session only in server memory; never give its cookie,
        # password, CSRF value or general account APIs to the local component.
        saved = self.accounts.request('POST', '/v1/session', json={'token': value['session']})
        user = saved['user']
        if user['id'] != value['owner'] or not user.get('can_upload'):
            raise HTTPException(403, '上传权限已关闭。')
        return user

    def owned(self, user, ident):
        with self.lock:
            value = self.agents.get(ident)
            if not value or value['owner'] != user['id'] or value['expires'] <= self.clock():
                raise HTTPException(404, '此浏览器尚未关联本机上传组件。')
            if not user.get('can_upload'):
                raise HTTPException(403, '上传权限已关闭。')
            return value

    def public(self, value):
        return {'id': value['id'], 'platform': value['platform'],
                'update_available':bool(self.current_version() and value.get('component_version')!=self.current_version()), 'online': self.clock()-value['seen'] < 15,
                'command': {k: value['command'][k] for k in ('id', 'action')} if value['command'] else None,
                'report': value['report']}

    def command(self, user, ident, body):
        value = self.owned(user, ident)
        action = body.get('action')
        fields = body.get('fields', {})
        if action not in {'select', 'start'} or not isinstance(fields, dict):
            raise HTTPException(422, '上传操作无效。')
        if set(fields)-{'collector','uploader','note','prediction','chest_device','thigh_device','upload_kind','capture_date'} or any(not isinstance(v,str) or len(v)>500 for v in fields.values()):
            raise HTTPException(422, '只允许采集信息，不允许远程指定本机路径或命令。')
        if fields.get('upload_kind', 'video') not in {'video', 'imu'}:
            raise HTTPException(422, '上传素材类型无效。')
        with self.lock:
            if self.clock()-value['seen'] >= 15:
                raise HTTPException(409, '本机组件已离线，请按安装／启动命令重新启动。')
            if value['command'] or value['report'].get('busy'):
                raise HTTPException(409, '本机正在选择、压缩或上传，请等待完成。')
            value.update(command={'id':uuid.uuid4().hex,'action':action,'fields':fields}, delivered=False)
            return self.public(value)

    def poll(self, value, body):
        report = body.get('report')
        if report is not None:
            # Reports are UI metadata only. No local path, credentials or signed
            # URLs are accepted into the relay's browser-visible state.
            import json
            if not isinstance(report, dict) or len(json.dumps(report)) > 512*1024:
                raise HTTPException(422, '组件进度数据过大。')
            allowed = {'busy','selection','job','error'}
            if set(report)-allowed:
                raise HTTPException(422, '组件进度格式无效。')
            if 'busy' in report and type(report['busy']) is not bool:
                raise HTTPException(422, '组件运行状态无效。')
            if 'error' in report and (not isinstance(report['error'],str) or len(report['error'])>2000):
                raise HTTPException(422, '组件错误信息无效。')
            selected=report.get('selection')
            if selected is not None:
                if (not isinstance(selected,dict) or set(selected)-{'upload_kind'}!={'name','capture_date','videos','attachments','ignored'}
                        or not isinstance(selected['name'],str) or '/' in selected['name'] or '\\' in selected['name']
                        or selected.get('upload_kind', 'video') not in ('video', 'imu')
                        or not isinstance(selected['capture_date'],str)
                        or not (selected.get('upload_kind') == 'imu' and selected['capture_date'] == ''
                                or selected['capture_date'].isdigit() and len(selected['capture_date']) == 8)):
                    raise HTTPException(422, '组件文件选择信息无效。')
                for key in ('videos','attachments','ignored'):
                    items=selected[key]
                    if (not isinstance(items,list) or len(items)>5000 or any(not isinstance(item,str) or len(item)>1000
                            or item.startswith('/') or '\\' in item or ':' in item for item in items)):
                        raise HTTPException(422, '组件不能上报本机绝对路径。')
        with self.lock:
            if report is not None:
                value['report'] = report
            if body.get('completed') and value['command'] and body['completed']==value['command']['id']:
                value.update(command=None,delivered=False)
            if body.get('received') and value['command'] and body['received']==value['command']['id']:
                value['delivered'] = True
            value['seen'] = self.clock()
            command = None
            if value['owner'] and value['command'] and not value['delivered']:
                self.actor(value)
                command = value['command']
            return {'paired':bool(value['owner']),'command':command}


def install(app, router, actor, accounts, uploads):
    bridge = UploadBridge(accounts,current_version=lambda:getattr(app.state,'uploader_source_key',None))

    @app.post('/api/r2/helper/register')
    def register(body: dict, request: Request):
        return bridge.register(body,request.client.host if request.client else 'unknown')

    @app.post('/api/r2/helper/poll')
    def poll(body: dict, request: Request):
        return bridge.poll(bridge.agent(request), body)

    @app.get('/api/r2/helper/identity')
    def identity(request: Request):
        return bridge.actor(bridge.agent(request))

    @router.post('/r2/helpers/pair')
    def pair(body: dict, request: Request):
        return bridge.pair(actor(request),body,request.cookies.get('datamark_session'))

    @router.get('/r2/helpers/{ident}')
    def status(ident: str, request: Request):
        value=bridge.owned(actor(request),ident)
        value['session']=request.cookies.get('datamark_session')
        return bridge.public(value)

    @router.post('/r2/helpers/{ident}/commands')
    def command(ident: str, body: dict, request: Request):
        return bridge.command(actor(request),ident,body)

    # Only upload operations accept the scoped component credential. Existing
    # project, account, draft and task routes still require browser sessions.
    @app.post('/api/r2/helper/transfer/uploads')
    def start(body: dict, request: Request):
        return uploads.start(bridge.actor(bridge.agent(request)),body)

    @app.get('/api/r2/helper/transfer/uploads/{ident}')
    def upload_status(ident: str, request: Request):
        return uploads.status(ident, bridge.actor(bridge.agent(request)))

    @app.get('/api/r2/helper/transfer/uploads/{ident}/files/{index}')
    def plan(ident: str,index: int,request: Request,single: bool = False):
        return uploads.plan(ident,index,bridge.actor(bridge.agent(request)),single=single)

    @app.post('/api/r2/helper/transfer/uploads/{ident}/files/{index}/complete')
    def complete(ident: str,index: int,request: Request):
        return uploads.complete_file(ident,index,bridge.actor(bridge.agent(request)))

    @app.post('/api/r2/helper/transfer/uploads/{ident}/finish')
    def finish(ident: str,request: Request,background: bool = False, prepare: bool = False):
        authorize = lambda: bridge.actor(bridge.agent(request))
        user = authorize()
        if background:
            return uploads.begin_finish(ident,user,authorize,prepare=prepare)
        return uploads.finish(ident,user,authorize=authorize,prepare=prepare)

    return bridge
