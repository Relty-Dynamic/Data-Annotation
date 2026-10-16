"""Authenticated R2 routes; reuse the existing project editor and shared accounts."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from .desktop_cache import safe_name
from .service import now

from .app import (DraftRequest, TrackSelectionRequest, RenameProjectRequest, WritebackRequest,
                  DeleteProjectRequest, EditingSessionRequest, CacheCollectionRequest, PreparationRequest,
                  SkipFailedRequest, ChangePasswordRequest, UserRequest, ActiveRequest, UploadPermissionRequest,
                  PasswordRequest, AssignmentRequest)
from .r2_service import R2ProjectService
from .r2_uploads import R2Uploads
from .tasks.storage import ProjectMedia


class R2Media(ProjectMedia):
    def __init__(self, service):
        super().__init__(service, 's3', service.allowed)

    def media_manifest(self, service, manifest):
        return self.service.media_manifest(service, manifest)

    def direct_session(self, project):
        from .upload_playback import direct_manifest
        return direct_manifest(self.service.provider, project)

    def collection_outputs(self,store,editor):
        from .tasks.collection_outputs import CollectionOutputs
        return CollectionOutputs(store,self,editor)

    def source_tools(self, user, action, body, request):
        if action == 'capabilities':
            return {'backend': 's3', 'can_pick': False, 'can_browse': True}
        if action == 'browse':
            return self.service.browse(body.get('path') or '')
        if action == 'inspect':
            return self.service.open_capture(body.get('path'), actor=user, inspect=True)
        if action == 'import':
            return self.service.open_capture(body.get('path'), actor=user, body=body)
        raise HTTPException(422, '公网只接受已上传的 S3 缓存目录；本机 AVI 请使用上传工具。')


def install(app, session, accounts, provider, root):
    service = R2ProjectService(root, provider)
    uploads = R2Uploads(root / '.local', provider, service)

    def authenticate(request: Request):
        request.state.r2_user = session(request)

    router = APIRouter(prefix='/api', dependencies=[Depends(authenticate)])

    def actor(request):
        return request.state.r2_user

    from .upload_bridge import install as install_upload_bridge
    app.state.upload_bridge = install_upload_bridge(app, router, actor, accounts, uploads)
    from pathlib import Path
    from .upload_install import install as install_upload_installer
    install_upload_installer(app, Path(__file__).resolve().parents[1])

    def project(ident, request):
        value = service.load(ident)
        if not service.allowed(actor(request), ident):
            raise HTTPException(403, '此项目已由其他账号认领。')
        return value

    def library(request):
        return accounts.request('POST', '/v1/axis-library', json={'token': request.cookies.get('datamark_session')})['axes']

    def administer(request, action, body=None):
        return accounts.request('POST', '/v1/admin/accounts', json={
            'token': request.cookies.get('datamark_session'), 'csrf': request.cookies.get('datamark_csrf'),
            'action': action, 'body': body or {}})['result']

    @router.post('/auth/password')
    def password(body: ChangePasswordRequest, request: Request):
        accounts.request('POST', '/v1/password', json={'token': request.cookies.get('datamark_session'),
            'csrf': request.cookies.get('datamark_csrf'), **body.model_dump()})
        response = JSONResponse({'ok': True})
        for name in ('datamark_session', 'datamark_csrf'):
            response.delete_cookie(name, path='/', secure=True, samesite='strict')
        return response

    @router.get('/users')
    def users(request: Request):
        return administer(request, 'list')

    @router.post('/users')
    def create_user(body: UserRequest, request: Request):
        return administer(request, 'editor-create', body.model_dump())

    @router.patch('/users/{user_id}')
    def active(user_id: str, body: ActiveRequest, request: Request):
        return administer(request, 'active', {'user_id': user_id, **body.model_dump()})

    @router.put('/users/{user_id}/upload-permission')
    def permission(user_id: str, body: UploadPermissionRequest, request: Request):
        return administer(request, 'upload', {'user_id': user_id, **body.model_dump()})

    @router.put('/users/{user_id}/password')
    def reset(user_id: str, body: PasswordRequest, request: Request):
        return administer(request, 'password', {'user_id': user_id, **body.model_dump()})

    @router.get('/projects/{ident}/assignment')
    def assignment(ident: str, request: Request):
        if actor(request).get('role') != 'admin':
            raise HTTPException(403, '只有管理员可调整领取人。')
        return {'user_id': project(ident, request).get('_r2_owner')}

    @router.put('/projects/{ident}/assignment')
    def assign(ident: str, body: AssignmentRequest, request: Request):
        if actor(request).get('role') != 'admin':
            raise HTTPException(403, '只有管理员可调整领取人。')
        directory = accounts.request('POST', '/v1/users', json={'token': request.cookies.get('datamark_session')})['users']
        if body.user_id is not None and not any(user['id'] == body.user_id and user['active'] for user in directory):
            raise HTTPException(422, '请选择有效账号。')
        with service.lock:
            value = project(ident, request)
            value.update(_r2_owner=body.user_id, _r2_owner_name=next((user['display_name'] for user in directory if user['id'] == body.user_id), None), revision=value['revision'] + 1, updated_at=now())
            service.save(value)
        return {'user_id': body.user_id}

    @router.get('/storage/sources')
    def sources(request: Request, path: str = '', page: int = 0):
        return service.browse(path)

    @router.post('/source-claims/inspect')
    def inspect(body: dict, request: Request):
        return service.open_capture(body.get('path'), actor=actor(request), inspect=True)

    @router.post('/storage/projects/open')
    def open_project(body: dict, request: Request):
        value = service.open_capture(body.get('path'), actor=actor(request), body=body)
        return service.initialize_track_library(value['id'], [axis for axis in library(request) if axis.get('is_default', True)])

    @router.get('/projects')
    def projects(request: Request):
        return [value for value in service.projects() if service.allowed(actor(request), value['id'])]

    @router.get('/account/track-library')
    def list_library(request: Request):
        return library(request)

    @router.post('/account/track-library')
    @router.put('/account/track-library/{track_id}')
    @router.delete('/account/track-library/{track_id}')
    @router.post('/account/track-library/{track_id}/default')
    async def edit_library(request: Request, track_id: str = '', default: bool = False):
        from .tasks.routes import enabled
        if enabled() and actor(request)['role'] != 'admin':
            raise HTTPException(403, '任务时间轴由标注规则统一管理，不能使用旧时间轴库自定义。')
        body = await request.json()
        return accounts.request('POST', '/v1/library-edit', json={
            'token': request.cookies.get('datamark_session'), 'csrf': request.cookies.get('datamark_csrf'),
            'method': request.method, 'track_id': track_id, 'default': default,
            'toggle_default': request.url.path.endswith('/default'), 'body': body})['result']

    @router.get('/projects/{ident}')
    def get_project(ident: str, request: Request):
        project(ident, request)
        return service.public(service.load(ident))

    @router.put('/projects/{ident}/draft')
    def save_draft(ident: str, body: DraftRequest, request: Request):
        body = body.model_dump()
        with service.lock:
            project(ident, request)
            return service.update_draft(ident, body.get('annotations'), body.get('expected_revision'), actor=actor(request))

    @router.put('/projects/{ident}/track-selection')
    def tracks(ident: str, body: TrackSelectionRequest, request: Request):
        from .tasks.routes import enabled
        if enabled() and actor(request)['role'] != 'admin':
            raise HTTPException(403, '任务时间轴由标注规则统一管理，不能自行更改。')
        body = body.model_dump()
        with service.lock:
            project(ident, request)
            return service.select_library_tracks(ident, body.get('tracks'), library(request), body.get('expected_revision'), actor=actor(request))

    @router.patch('/projects/{ident}/name')
    def rename(ident: str, body: RenameProjectRequest, request: Request):
        body = body.model_dump()
        with service.lock:
            project(ident, request)
            return service.rename_project(ident, body.get('name'), body.get('expected_revision'))

    @router.get('/projects/{ident}/history')
    @router.get('/projects/{ident}/operations')
    def history(ident: str, request: Request):
        project(ident, request)
        return service.project_operation_history(ident) if request.url.path.endswith('/operations') else service.edit_history(ident)

    @router.post('/projects/{ident}/editing')
    @router.delete('/projects/{ident}/editing')
    def editing(ident: str, body: EditingSessionRequest, request: Request):
        body = body.model_dump()
        project(ident, request)
        return service.editing(ident, actor(request), body.get('tab_id'), leave=request.method == 'DELETE')

    @router.get('/projects/{ident}/export')
    def export(ident: str, request: Request):
        project(ident, request)
        return Response(service.export_zip(ident), media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="timelines-{ident}.zip"'})

    @router.get('/projects/{ident}/export-documents')
    def export_documents(ident: str, request: Request):
        project(ident, request)
        return service.export_files(ident)

    @router.post('/projects/{ident}/writeback')
    def writeback(ident: str, body: WritebackRequest, request: Request):
        body = body.model_dump()
        with service.lock:
            project(ident, request)
            return service.writeback(ident, expected_revision=(body or {}).get('expected_revision'))

    @router.get('/projects/{ident}/session')
    def status(ident: str, request: Request):
        project(ident, request)
        return service.sessions.status(ident)

    @router.post('/projects/{ident}/session/prepare')
    def prepare(ident: str, request: Request, body: PreparationRequest | None = None):
        body = body.model_dump() if body else {}
        project(ident, request)
        return service.start_session(ident, retry_failed=(body or {}).get('retry_failed', False), cache_generation=(body or {}).get('cache_generation'))

    @router.post('/projects/{ident}/session/skip-failed')
    def skip_failed(ident: str, body: SkipFailedRequest, request: Request):
        body = body.model_dump()
        project(ident, request)
        return service.skip_failed_videos(ident, body.get('video_ids'), body.get('expected_revision'))

    @router.post('/projects/{ident}/session/restore-skipped')
    def restore_skipped(ident: str, body: DeleteProjectRequest, request: Request):
        body = body.model_dump()
        project(ident, request)
        return service.restore_skipped_videos(ident, body.get('expected_revision'))

    @router.get('/projects/{ident}/session/manifest')
    def manifest(ident: str, request: Request):
        project(ident, request)
        return service.media_manifest(service, service.sessions.manifest(ident))

    @router.post('/projects/{ident}/session/browser-ready')
    def browser_ready(ident: str, request: Request):
        project(ident, request)
        return service.sessions.browser_ready(ident)

    @router.get('/previews/{ident}/{video}')
    def preview(ident: str, video: str, request: Request, fast: bool = False):
        project(ident, request)
        state = service.sessions.status(ident)
        if state['state'] != 'ready':
            return {'state': state['state'], 'progress': state.get('progress'), 'detail': state.get('detail'), 'url': None}
        value = service.media_manifest(service, service.sessions.manifest(ident))
        found = next((item for item in value['videos'] if item['id'] == video), None)
        if found is None:
            raise HTTPException(404, '视频不存在。')
        return {'state': 'ready', 'progress': 100, 'url': found['fast_url'] if fast else found['url']}

    @router.put('/projects/{ident}/cache-collection')
    def cache_collection(ident: str, body: CacheCollectionRequest, request: Request):
        body = body.model_dump()
        name = body.get('name')
        if not isinstance(name, str) or not name or len(name.encode()) > 100 or safe_name(name) != name:
            raise HTTPException(422, '采集目录名不能使用路径、系统保留名或 Windows 不支持的字符。')
        with service.lock:
            value = project(ident, request)
            if body.get('expected_revision') != value['revision']:
                raise HTTPException(409, '项目已更新，请重新打开后设置采集目录名。')
            if value['collection_folder_name'] != name:
                service.sessions.invalidate(ident)
                value.update(collection_folder_name=name, revision=value['revision'] + 1, updated_at=now())
                service.save(value)
            return service.public(value)

    @router.get('/cache-location')
    def cache_location(request: Request):
        return service.desktop_cache.settings()

    @router.post('/projects/{ident}/preview-cache/clear')
    def clear(ident: str, body: DeleteProjectRequest, request: Request):
        body = body.model_dump()
        project(ident, request)
        if body.get('confirmed') is not True:
            raise HTTPException(422, '请确认清理本机预览。')
        with service.lock:
            project(ident, request)
            return service.clear_local_previews(ident, body.get('expected_revision'))

    @router.post('/r2/uploads')
    def start_upload(body: dict, request: Request):
        return uploads.start(actor(request), body)

    @router.get('/r2/uploads/{ident}')
    def upload_status(ident: str, request: Request):
        return uploads.public(uploads.load(ident, actor(request)))

    @router.get('/r2/uploads/{ident}/files/{index}')
    def upload_plan(ident: str, index: int, request: Request):
        return uploads.plan(ident, index, actor(request))

    @router.post('/r2/uploads/{ident}/files/{index}/complete')
    def upload_complete(ident: str, index: int, request: Request):
        return uploads.complete_file(ident, index, actor(request))

    @router.post('/r2/uploads/{ident}/finish')
    def upload_finish(ident: str, request: Request):
        return uploads.finish(ident, actor(request))

    app.include_router(router)
    app.state.r2_service, app.state.r2_uploads = service, uploads
    return service, R2Media(service)
