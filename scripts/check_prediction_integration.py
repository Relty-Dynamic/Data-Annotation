"""Counterpart of IMU_posture/scripts/check_remote_datamark.py; mock storage only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from backend.prediction_gateway import install
from backend.r2_routes import R2Media
from backend.r2_service import R2ProjectService
from backend.r2_uploads import R2Uploads
from backend.service import ProjectService
from backend.tasks.storage import FileArtifacts
from backend.tasks.store import TaskStore
from backend.tasks.workspace import Workspaces
from backend.test_r2_integration import MultipartObjects, USER
from backend.upload_agent import AgentEngine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('capture', 'expected', 'token-file', 'work'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--worker-url', required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True)
    env = dict(DATAMARK_ORIGIN='', DATAMARK_DESKTOP_CACHE='1',
        DATAMARK_DESKTOP_CACHE_ROOT=str(args.work / 'desktop/dataMarkProject'),
        DATAMARK_PREDICTION_URL=args.worker_url, DATAMARK_PREDICTION_TOKEN_FILE=str(args.token_file))
    with patch.dict(os.environ, env):
        local = ProjectService(args.work / 'local', enable_remote=False)
        provider = MultipartObjects()
        public = R2ProjectService(args.work / 'public', provider)
        uploads = R2Uploads(public.local, provider, public)
        for service in (local, public):
            service.tool = lambda name: Path(shutil.which(name))
        app, router = FastAPI(), APIRouter(prefix='/api')
        # Ephemeral test identity only; production uses the real shared session
        # and agent permission checks exercised by backend.test_upload_bridge.
        class Bridge:
            def actor(self, value): return USER
            def agent(self, request): return None
        install(app, router, lambda request: USER, Bridge())
        app.include_router(router)
        direct_client = httpx.Client
        signed = provider.signed_url
        provider.signed_url = lambda key, **kw: (f'https://parts.invalid/{kw["UploadId"]}/{kw["PartNumber"]}'
            if kw.get('operation') == 'upload_part' else signed(key, **kw))
        def direct(request):
            ident, number = request.url.path.strip('/').split('/')
            provider.uploads[ident]['parts'][int(number)] = request.content
            return httpx.Response(200)
        with TestClient(app) as gateway:
            class Client:
                def request(self, method, path, **kwargs):
                    if path == '/api/r2/helper/identity':
                        return httpx.Response(200, json=USER)
                    if path.startswith('/api/r2/helper/prediction/'):
                        return gateway.request(method, path, **kwargs)
                    path = path.replace('/api/r2/helper/transfer/uploads', '/api/r2/uploads', 1)
                    parts = path.split('/')
                    if path == '/api/r2/uploads': value = uploads.start(USER, kwargs['json'])
                    elif parts[-1] == 'finish': value = uploads.finish(parts[4], USER)
                    elif parts[-1] == 'complete': value = uploads.complete_file(parts[4], int(parts[6]), USER)
                    elif len(parts) == 5: value = uploads.public(uploads.load(parts[4], USER))
                    else: value = uploads.plan(parts[4], int(parts[6]), USER)
                    return httpx.Response(200, json=value)
            engine = AgentEngine(args.work, Client(), service=local, picker=lambda root, kind: [str(args.capture)])
            try:
                engine.execute(dict(id='select', action='select', fields={}))
                assert not engine.report.get('error'), engine.report
                assert not any(a == 'prediction/posture.json' for a in engine.report['selection']['attachments'])
                with patch('backend.r2_upload.httpx.Client', side_effect=lambda **kw: direct_client(transport=httpx.MockTransport(direct), **kw)):
                    engine.execute(dict(id='start', action='start', fields=dict(collector='fixture', prediction='generate',
                        chest_device='WT901BLE68', thigh_device='WT901BLE67')))
                assert not engine.report.get('error'), engine.report
                assert engine.snapshot()['job']['state'] == 'ready', engine.snapshot()
                prefix = engine.job['prefix']
                published = provider.optional_json(prefix + '/manifest.json')[0]
                attachments = {item['name']: item for item in published['attachments']}
                prediction = attachments['prediction/posture.json']
                raw = provider.get(prefix + '/' + prediction['key'])
                assert raw == (args.capture / 'prediction/posture.json').read_bytes()
                output, expected = json.loads(raw), json.loads(args.expected.read_bytes())
                for key in ('segments', 'recordings', 'prediction_gaps', 'timebase'):
                    assert output[key] == expected[key], key
                assert all(not key.lower().endswith('.avi') for key in provider.objects)
                assert any(item['name'] == 'IMU/session.txt' for item in published['attachments'])
                from backend.prediction_prepare import prepare_prediction
                from backend.r2_folder import scan_collection
                _, reused = prepare_prediction(engine.target, local, args.capture, scan_collection(args.capture),
                    dict(chest='WT901BLE68', thigh='WT901BLE67'))
                assert reused['sha256'] == prediction['sha256']
                assert (args.capture / 'prediction/posture.json').read_bytes() == raw
                opened = public.open_capture(prefix, actor=USER)
                public.initialize_track_library(opened['id'], [dict(id='posture', name='姿势', mode='state',
                    labels=['坐', '站', '躺', '移动'])])
                opened = public.load(opened['id'])
                assert opened['annotations']['posture'], opened.get('prediction_import')
                assert opened['prediction_import']['sha256'] == hashlib.sha256(raw).hexdigest()
                public.update_draft(opened['id'], {**opened['annotations'], 'posture': []}, opened['revision'], actor=USER)
                again = public.open_capture(prefix, actor=USER)
                assert again['annotations']['posture'] == []
                store = TaskStore(args.work / 'tasks', FileArtifacts(args.work / 'artifacts'))
                admin = dict(id='admin-fixture', role='admin', can_upload=True)
                actor, reviewer = dict(id='a', role='annotator'), dict(id='r', role='annotator')
                store.set_roles(admin, 'a', ['annotator']); store.set_roles(admin, 'r', ['reviewer'])
                media = R2Media(public)
                records = [media.reference(USER, dict(id=row['id'].replace('/', '-'), collector_id='fixture',
                    date='2026-01-01', scene='室内', quality='passed', duration_ms=row['duration_ms'], source=row['source']))
                    for row in media.catalog(USER)]
                store.register_collection(admin, records)
                rule = store.publish_rule(admin, dict(name='fixture', task_type='posture',
                    axes=[dict(id='posture', name='姿势', mode='state', labels=['坐', '站', '躺', '移动'])]))
                body = dict(collection_id=opened['id'], storage_backend='s3', rule_id=rule['id'],
                    annotator_ids=['a'], reviewer_id='r', deadline='2026-12-31')
                members, _ = store.task_materials(admin, body)
                task = store.create_task(admin, body, collection_layout=media.task_collection(members))
                workspace = Workspaces(args.work / 'workspace', store, media)
                try:
                    task_project = workspace.public(actor, task['id'])
                    assert task_project['annotations']['posture']
                    assert task_project['prediction_import']['sha256'] == hashlib.sha256(raw).hexdigest()
                    revision = task_project['revision']
                    assert workspace.public(actor, task['id'])['revision'] == revision
                    saved = workspace.save(actor, task['id'], dict(expected_revision=revision, annotations={'posture': []}))
                    assert saved['annotations']['posture'] == []
                finally:
                    workspace.close()
                print('PASS: actual remote inference matches original local output; agent upload, verified mock manifest, project/task initialization, and human-draft preservation')
            finally:
                engine.close(); public.sessions.close(); public.previews.close()


if __name__ == '__main__':
    main()
