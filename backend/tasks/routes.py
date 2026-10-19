"""One optional task router for NAS, mock and future S3 application hosts."""
from __future__ import annotations
import os
import re
import sqlite3
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, RedirectResponse
from ..media_response import CancellableFileResponse
from .storage import FileArtifacts, UnavailableMedia
from .workspace import Workspaces


def enabled():
    value = os.getenv('DATAMARK_TASKS_ENABLED', '0').strip()
    if value not in ('0', '1', ''):
        raise ValueError('DATAMARK_TASKS_ENABLED must be 0 or 1')
    return value == '1'


def install(app, actor_for_request, users_for_request, media=None, axis_library=None, accounts=None, source_tools=None, artifact_store=None):
    if not enabled():
        return None
    state, artifacts = os.getenv('DATAMARK_TASKS_STATE', ''), os.getenv('DATAMARK_TASKS_ARTIFACTS', '')
    if not state or not artifacts or not Path(state).is_absolute() or not Path(artifacts).is_absolute():
        raise ValueError('Enabled tasks require absolute DATAMARK_TASKS_STATE and DATAMARK_TASKS_ARTIFACTS')
    if any(p.is_symlink() for p in (Path(state), *Path(state).parents)):
        raise ValueError('Task state must not contain symlinks')
    from .store import TaskStore, TaskError
    rule_setting = os.getenv('DATAMARK_TASK_RULES_PUBLIC', '0').strip()
    if rule_setting not in ('0', '1', ''):
        raise ValueError('DATAMARK_TASK_RULES_PUBLIC must be 0 or 1')
    if rule_setting == '1' and os.getenv('DATAMARK_SHARED_ACCOUNTS') == '1':
        raise ValueError('The public host must remain the authoritative local rule store')
    from .public_rules import PublicRules
    public_rules = PublicRules() if rule_setting == '1' else None
    store = TaskStore(state, artifact_store or FileArtifacts(artifacts), public_rules=bool(public_rules))
    workspace = Workspaces(Path(state) / 'workspaces', store, media or UnavailableMedia())
    if hasattr(workspace.media,'collection_outputs'):
        store.outputs = workspace.media.collection_outputs(store,workspace.service)
    router = APIRouter(prefix='/api/tasks')

    @app.exception_handler(TaskError)
    async def task_error(request, error):
        return JSONResponse(status_code=error.status, content={'detail': error.detail}, headers={'Cache-Control':'no-store'})

    @app.exception_handler(OSError)
    @app.exception_handler(sqlite3.Error)
    async def persistence_error(request, error):
        if not request.url.path.startswith('/api/tasks/'):
            raise error
        return JSONResponse(status_code=503, content={'detail': '任务存储暂时不可用，当前操作未确认成功，请重试。'}, headers={'Cache-Control':'no-store'})

    def actor(request):
        return actor_for_request(request)

    def require_users(request, ids):
        available = {u['id']: u for u in users_for_request(request) if u.get('active', True)}
        if any(not isinstance(user_id, str) or user_id not in available for user_id in ids):
            raise HTTPException(422, '请选择仍有效的账号。')
        return available

    def require_participants(request, body, assignment=False):
        participants = body.get('annotator_ids', [])
        if not isinstance(participants, list):
            raise HTTPException(422, '标注员必须为账号列表。')
        reviewer = body.get('reviewer_id')
        return require_users(request, participants + ([reviewer] if reviewer or assignment else []))

    def admin(request):
        user = actor(request)
        if user.get('role') != 'admin':
            raise HTTPException(403, '只有超级管理员可管理账号。')
        if not accounts:
            raise HTTPException(503, '账号管理服务尚未配置。')
        return user

    def grant_roles(user, account, roles, request):
        granted = store.set_roles(user, account['id'], roles)
        can_upload = account.get('role') == 'admin' or 'leader' in granted['roles']
        if account.get('role') != 'admin':
            try:
                if not accounts:
                    raise HTTPException(503, '账号管理服务尚未配置。')
                accounts('upload', user, {'user_id': account['id'], 'can_upload': can_upload}, request)
            except (HTTPException, OSError, sqlite3.Error):
                raise HTTPException(503, {'message': '任务角色已保存，但上传权限未确认更新。请重新保存角色或单独调整上传权限。',
                                          'roles_saved': True, 'user_id': account['id']})
        return {**granted, 'can_upload': can_upload}

    @router.get('/accounts')
    def account_list(request: Request):
        user = admin(request)
        grants = {item['user_id']: item['roles'] for item in store.list_roles(user)}
        return [{**account, 'task_roles': grants.get(account['id'], [])}
                for account in accounts('list', user, {}, request)]

    @router.post('/accounts')
    def account_create(body: dict, request: Request):
        user = admin(request)
        roles = body.get('roles')
        if not isinstance(roles, list) or not roles or any(role not in ('annotator', 'reviewer', 'leader') for role in roles):
            raise HTTPException(422, '请至少选择一个有效任务角色。')
        account = accounts('create', user, body, request)
        try:
            granted = grant_roles(user, account, roles, request)
        except HTTPException as error:
            detail = error.detail if isinstance(error.detail, dict) else {'message': str(error.detail)}
            raise HTTPException(503, {**detail, 'message': '账号已创建。' + detail['message'], 'account_created': True})
        except (TaskError, OSError, sqlite3.Error):
            # Two stores cannot commit atomically across the private account bridge.
            # Preserve the new account and clearly report the remaining authorization.
            raise HTTPException(503, {'message': '账号已创建，但任务角色未确认保存。请在账号管理中点击角色补设，不要重复创建账号。',
                                      'account_created': True, 'user_id': account['id']})
        return {**account, 'task_roles': granted['roles'], 'can_upload': granted['can_upload']}

    @router.put('/accounts/{user_id}/{action}')
    def account_update(user_id: str, action: str, body: dict, request: Request):
        user = admin(request)
        if action not in ('upload', 'active', 'password'):
            raise HTTPException(404, '账号操作不存在。')
        return accounts(action, user, {**body, 'user_id': user_id}, request)

    @router.delete('/accounts/{user_id}')
    def account_delete(user_id: str, request: Request):
        user = admin(request)
        return accounts('delete', user, {'user_id': user_id}, request)

    @router.get('/me')
    def me(request: Request):
        user = actor(request)
        return {**store.roles(user), 'can_upload': user.get('can_upload', False), 'admin': user.get('role') == 'admin'}

    @router.get('/users')
    def users(request: Request):
        user = actor(request)
        if user.get('role') != 'admin' and 'leader' not in store.roles(user)['roles']:
            raise HTTPException(403, '只有管理员或组长可查看分配账号。')
        return [{k: u.get(k) for k in ('id','display_name','role','active','can_upload')} for u in users_for_request(request)]

    @router.get('/roles')
    def list_roles(request: Request):
        return store.list_roles(actor(request))

    @router.put('/roles')
    def roles(body: dict, request: Request):
        user = actor(request)
        if user.get('role') != 'admin':
            raise HTTPException(403, '只有超级管理员可授权任务角色。')
        roster = require_users(request, [body.get('user_id')])
        return grant_roles(user, roster[body['user_id']], body.get('roles'), request)

    @router.get('/rules')
    def rules(request: Request):
        if public_rules:
            if not store.roles(actor(request))['roles']:
                raise HTTPException(403, '需要任务角色。')
            return public_rules.request(request)
        return store.list_rules(actor(request))

    @router.get('/axis-library')
    def library(request: Request):
        user = actor(request)
        if 'leader' not in store.roles(user)['roles']:
            raise HTTPException(403, '只有组长可复制时间轴库定义。')
        if axis_library:
            return axis_library(user, request)
        from ..service import DEFAULT_TRACK_LABELS, AXIS_NAMES
        return [{'id': key, 'name': AXIS_NAMES[key], 'mode': 'state', 'labels': labels}
                for key, labels in DEFAULT_TRACK_LABELS.items() if key in ('scene','posture','category')]

    @router.post('/rules')
    def publish(body: dict, request: Request):
        if public_rules:
            if 'leader' not in store.roles(actor(request))['roles']:
                raise HTTPException(403, '缺少任务角色：leader')
            return public_rules.request(request, 'POST', body=body)
        return store.publish_rule(actor(request), body)

    @router.delete('/rules/{rule_id}')
    def delete_rule(rule_id: str, request: Request):
        if public_rules:
            if actor(request).get('role') != 'admin':
                raise HTTPException(403, '只有超级管理员可删除已发布规则')
            return public_rules.request(request, 'DELETE', rule_id=rule_id)
        return store.delete_rule(actor(request), rule_id)

    @router.get('/videos')
    def videos(request: Request):
        return store.list_videos(actor(request))

    @router.get('/sources')
    def sources(request: Request):
        return workspace.media.catalog(actor(request))

    def source_actor(request):
        user = actor(request)
        if not user.get('can_upload'):
            raise HTTPException(403, '管理员尚未开放上传权限。')
        if not source_tools:
            raise HTTPException(503, '真实 S3 素材尚未接入，暂不能导入视频。')
        return user

    @router.get('/sources/capabilities')
    def source_capabilities(request: Request):
        return source_tools(source_actor(request), 'capabilities', {}, request)

    @router.get('/sources/browse')
    def source_browse(request: Request, path: str | None = None, page: int = 0):
        return source_tools(source_actor(request), 'browse', {'path': path, 'page': page}, request)

    @router.post('/sources/pick')
    def source_pick(body: dict, request: Request):
        return source_tools(source_actor(request), 'pick', body, request)

    @router.post('/sources/inspect')
    def source_inspect(body: dict, request: Request):
        return source_tools(source_actor(request), 'inspect', body, request)

    @router.post('/sources/import')
    def source_import(body: dict, request: Request):
        user = source_actor(request)
        project = source_tools(user, 'import', body, request)
        sources = [source for source in workspace.media.catalog(user) if source['id'].startswith(project['id'] + '/')]
        return {'project_id': project['id'], 'collection': project['name'], 'sources': sources}

    @router.post('/videos')
    def video(body: dict, request: Request):
        user = actor(request)
        if not user.get('can_upload'):
            raise HTTPException(403, '管理员尚未开放上传权限。')
        return store.register_video(user, workspace.media.reference(user, body))

    @router.post('/collections')
    def register_collection(body: dict, request: Request):
        user = actor(request)
        if not user.get('can_upload'):
            raise HTTPException(403, '管理员尚未开放上传权限。')
        collection_id = body.get('collection_id')
        if not isinstance(collection_id, str) or not re.fullmatch(r'[0-9a-f]{32}', collection_id):
            raise HTTPException(422, '请选择有效的采集文件夹。')
        sources = [source for source in workspace.media.catalog(user)
                   if source['id'].split('/')[0] == collection_id]
        if not sources:
            raise HTTPException(404, '采集文件夹不存在或未获授权。')
        records = [workspace.media.reference(user, {
            'id': source['id'].replace('/', '-'),
            'collector_id': body.get('collector_id'), 'date': body.get('date'),
            'scene': body.get('scene'), 'quality': body.get('quality', 'unknown'),
            'duration_ms': source['duration_ms'], 'source': source['source'],
        }) for source in sources]
        return {**store.register_collection(user, records), 'collection': sources[0]['collection']}

    @router.post('/videos/batch')
    def video_batch(body: dict, request: Request):
        user = actor(request)
        items = body.get('videos')
        if not isinstance(items, list) or not 1 <= len(items) <= 1000:
            raise HTTPException(422, '请提供 1 至 1000 条视频记录。')
        if not user.get('can_upload'):
            raise HTTPException(403, '管理员尚未开放上传权限。')
        # Each item returns its own outcome; no apparent all-or-nothing claim.
        results = []
        for item in items:
            if not isinstance(item, dict):
                results.append({'ok': False, 'detail': '视频记录必须为对象。'})
                continue
            try:
                results.append({'ok': True, 'video': store.register_video(user, workspace.media.reference(user, item))})
            except (HTTPException, TaskError) as error:
                results.append({'ok': False, 'id': item.get('id'), 'detail': error.detail})
        return {'results': results}

    @router.put('/videos/{video_id}/quality')
    def quality(video_id: str, body: dict, request: Request):
        return store.set_quality(actor(request), video_id, body.get('quality'))

    @router.get('/items')
    def tasks(request: Request, include_deleted: bool = False):
        return store.list_tasks(actor(request), include_deleted=include_deleted)

    @router.post('/items')
    def create(body: dict, request: Request):
        user = actor(request)
        roster = require_participants(request, body)
        members, collection_key = store.task_materials(user, body)
        layout = workspace.media.task_collection(members) if collection_key else None
        rule = public_rules.for_task(request, body.get('rule_id')) if public_rules else None
        return store.create_task(user, body, participants=roster, collection_layout=layout, rule_snapshot=rule)

    @router.get('/items/{task_id}')
    def task(task_id: str, request: Request):
        detail = store.get_task(actor(request), task_id)
        ids = {*detail['annotator_ids'], detail.get('reviewer_id'), detail.get('adjudicated_by'), detail.get('reviewed_by'), detail['video'].get('uploader_id'), *[note['actor_id'] for note in detail.get('review_notes', [])]}
        detail['participants'] = [{'id': user['id'], 'display_name': user['display_name']}
                                  for user in users_for_request(request) if user['id'] in ids]
        return detail

    @router.get('/items/{task_id}/history')
    def history(task_id: str, request: Request):
        return store.history(actor(request), task_id)

    @router.delete('/items/{task_id}')
    def delete_task(task_id: str, body: dict, request: Request):
        return store.delete_task(actor(request), task_id, body)

    @router.put('/items/{task_id}/assignment')
    def assign(task_id: str, body: dict, request: Request):
        user = actor(request)
        roster = require_participants(request, body, assignment=True)
        return store.assign_task(user, task_id, body, participants=roster)

    @router.put('/items/{task_id}/draft')
    def draft(task_id: str, body: dict, request: Request):
        return store.save_draft(actor(request), task_id, body)

    for action, method in [('submit','submit'), ('start-review','start_review'), ('review','review'), ('adjudicate','adjudicate'), ('accept','accept')]:
        def make_endpoint(method):
            def endpoint(task_id: str, body: dict, request: Request):
                names = {user['id']:user['display_name'] for user in users_for_request(request)} if store.outputs is not None and method in ('submit','accept') else None
                result = getattr(store,method)(actor(request),task_id,body,**({'output_names':names} if names is not None else {}))
                if store.outputs is not None:
                    result['output_sync'] = store.outputs.flush(task_id)
                return result
            return endpoint
        router.add_api_route('/items/{task_id}/' + action, make_endpoint(method), methods=['POST'])

    @router.post('/items/{task_id}/outputs/retry')
    def retry_outputs(task_id: str, request: Request):
        if store.outputs is None:
            raise HTTPException(404,'此存储模式不使用 S3 采集结果目录。')
        names = {user['id']:user['display_name'] for user in users_for_request(request)}
        return store.outputs.backfill(actor(request),task_id,names)

    @router.get('/audit')
    def audit(request: Request, task_id: str | None = None):
        return store.audit(actor(request), task_id)

    @router.get('/deliveries')
    def deliveries(request: Request):
        return store.list_deliveries(actor(request))

    @router.post('/deliveries')
    def deliver(body: dict, request: Request):
        names = {user['id']:user['display_name'] for user in users_for_request(request)} if store.outputs is not None else None
        result = store.deliver(actor(request),body,**({'output_names':names} if names is not None else {}))
        if store.outputs is not None:
            result['output_sync'] = [store.outputs.flush(ident) for ident in result['task_ids']]
        return result

    @router.get('/deliveries/{ident}')
    def download(ident: str, request: Request):
        record = store.delivery(actor(request), ident)
        return Response(store.artifacts.get(record['artifact_key']), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="tasks-{ident}.csv"', 'Cache-Control': 'no-store'})

    @router.api_route('/items/{task_id}/media', methods=['GET','HEAD'])
    def review_media(task_id: str, request: Request):
        user = actor(request)
        project = workspace.project(user, task_id)
        ident = project['id']
        state = workspace.session_status(project)
        if state['state'] != 'ready':
            if state['state'] == 'idle':
                workspace.session_status(project, start=True)
            return JSONResponse(status_code=202, content=workspace.session_status(project), headers={'Retry-After':'1'})
        if hasattr(workspace.media, 'media_manifest'):
            manifest = workspace.session_manifest(project, task_id)
            return RedirectResponse(manifest['videos'][0]['url'], status_code=307)
        path = workspace.service.sessions.asset(ident, 'normal', 'v0001')
        return CancellableFileResponse(path, media_type='video/mp4')

    @router.api_route('/items/{task_id}/workspace/{resource}/{tail:path}', methods=['GET','HEAD','POST','PUT','DELETE'])
    @router.api_route('/items/{task_id}/workspace/{resource}', methods=['GET','HEAD','POST','PUT','DELETE'])
    async def editor(task_id: str, resource: str, request: Request, tail: str = ''):
        user = actor(request)
        project = workspace.project(user, task_id)
        ident = project['id']
        service = workspace.service
        body = await request.json() if request.method in ('POST','PUT') and request.headers.get('content-type','').startswith('application/json') else {}
        if resource == 'projects' and not tail and request.method == 'GET':
            return [workspace.public(user, task_id)]
        if resource == 'project':
            if not tail and request.method == 'GET':
                return workspace.public(user, task_id)
            if tail == 'draft' and request.method == 'PUT':
                return workspace.save(user, task_id, body)
            if tail == 'editing':
                return {'others': []}
            if tail == 'session' and request.method == 'GET':
                return workspace.session_status(project)
            if tail == 'session/prepare' and request.method == 'POST':
                return workspace.session_status(project, start=True, retry_failed=body.get('retry_failed', False))
            if tail == 'session/manifest' and request.method == 'GET':
                return workspace.session_manifest(project, task_id)
            if tail == 'session/browser-ready' and request.method == 'POST':
                return {'released': False} if workspace.direct_session(project) is not None else service.sessions.browser_ready(ident)
            if tail in ('history', 'operations') and request.method == 'GET':
                return []
        if resource == 'previews' and any(video['id'] == tail for video in project['videos']):
            state = workspace.session_status(project)
            if state['state'] == 'ready':
                manifest = workspace.session_manifest(project, task_id)
                video = next(item for item in manifest['videos'] if item['id'] == tail)
                return {'state': 'ready', 'progress': 100, 'url': video['fast_url'] if request.query_params.get('fast') == 'true' else video['url']}
            return {'state': 'queued', 'progress': None, 'url': None, 'detail': state.get('detail')}
        match = re.fullmatch(r'([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)(?:/(\d+))?', tail)
        if resource in ('session-media','session-thumbnails','session-storyboards') and match and request.method in ('GET','HEAD'):
            version, video, index = match.groups()
            kind = 'normal' if resource == 'session-media' else 'thumbnail' if resource == 'session-thumbnails' else 'sheet'
            if kind == 'normal' and request.query_params.get('fast') == 'true':
                kind = 'fast'
            path = service.sessions.asset(ident, kind, video, int(index) if index else None, version=version)
            if path is None:
                raise HTTPException(409, '播放素材尚未就绪。')
            return CancellableFileResponse(path, media_type='video/mp4' if kind in ('normal','fast') else 'image/jpeg')
        raise HTTPException(404, '任务工作台不提供此项目操作。')

    app.include_router(router)
    app.state.tasks = store
    app.state.task_workspaces = workspace
    return workspace
