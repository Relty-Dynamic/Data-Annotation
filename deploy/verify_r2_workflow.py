"""Live R2 technical verification, confined to a unique test object prefix.

Uses generated media and disposable account databases, never production users.
Retains test objects as evidence; does not publish a real daily collection.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from unittest.mock import patch
import uuid

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.auth import AuthStore
from backend.mock_s3 import MockUploadManager
from backend.public_setup import create_app
from backend.r2_store import R2Store
from backend.r2_upload import RemoteCaptureTarget
from backend.service import ProjectService
from backend.shared_accounts import AccountClient, create_app as account_app


class TestObjects(R2Store):
    """Use actual R2 operations while containing every object in one test prefix."""
    def __init__(self, config, prefix):
        super().__init__(config)
        self.test_prefix = prefix

    def call(self, operation, **arguments):
        if 'Key' in arguments:
            arguments['Key'] = self.test_prefix + arguments['Key']
        if operation == 'list_objects_v2':
            arguments['Prefix'] = self.test_prefix + arguments.get('Prefix', '')
        return super().call(operation, **arguments)

    def signed_url(self, key, **arguments):
        return super().signed_url(self.test_prefix + key, **arguments)


def checked(response, action, status=200):
    if response.status_code != status:
        raise RuntimeError(f'{action}: unexpected HTTP {response.status_code}')
    return response.json() if status == 200 else None


def verify(config):
    prefix = '_datamark-checks/' + uuid.uuid4().hex + '/'
    provider = TestObjects(config, prefix)
    provider.health()
    origin = 'https://datamark.reltydynamic.com'
    api = 'https://api-annotate.reltydynamic.com:8443'
    password = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix='datamark-live-r2-') as temporary:
        root = Path(temporary)
        auth = AuthStore(root / 'accounts')
        auth.create_user('admin', 'Technical fixture admin', password, 'admin')
        worker = auth.create_user('worker', 'Technical fixture uploader', password)
        auth.create_user('other', 'Technical fixture other account', password)
        auth.set_upload_permission(worker['id'], True)
        video = root / 'capture/video/A00001_20261015120000_0001.avi'
        video.parent.mkdir(parents=True)
        subprocess.run(['/usr/bin/ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc=size=64x48:rate=5', '-t', '1', '-c:v', 'mpeg4', str(video)], check=True)
        uploader = ProjectService(root / 'uploader', enable_remote=False)
        uploader.desktop_cache.enabled = True
        uploader.desktop_cache.override = str(root / 'Desktop/dataMarkProject')
        environment = {'DATAMARK_PUBLIC_ORIGIN': origin, 'DATAMARK_PUBLIC_API_ORIGIN': api,
                       'DATAMARK_R2_CONFIG': str(config), 'DATAMARK_R2_STATE': str(root / 'public'),
                       'DATAMARK_TASKS_ENABLED': '0', 'FFMPEG_PATH': '/usr/bin/ffmpeg',
                       'FFPROBE_PATH': '/usr/bin/ffprobe'}
        try:
            with patch.dict(os.environ, environment), patch('backend.r2_store.R2Store', return_value=provider):
                bridge = TestClient(account_app(root / 'accounts'), base_url='http://accounts:8766')
                with TestClient(create_app(accounts=AccountClient(bridge)), base_url=api) as client:
                    login = checked(client.post('/api/auth/login', headers={'Origin': origin},
                        json={'username': 'worker', 'password': password}), 'fixture login')
                    client.headers.update({'Origin': origin, 'X-CSRF-Token': login['csrf']})
                    target = RemoteCaptureTarget(client, uploader)
                    manager = MockUploadManager(target, root / 'upload-staging')
                    selection = manager.select_local(worker['id'], [str(video)])
                    job = manager.start(login['user'], 'technical_verification', None, None, selection['selection_id'])
                    manager.finish(job['id'], worker['id'], uploader,
                                   lambda ident: target.request('GET', '/api/auth/me')['can_upload'])
                    capture = job['prefix']
                    manifest, _ = provider.optional_json(capture + '/manifest.json')
                    if not manifest or len(manifest['files']) != 1:
                        raise RuntimeError('compressed collection was not published')
                    print('PASS local compression, desktop retention, signed multipart upload and verified publication', flush=True)
                    project = target.request('POST', '/api/storage/projects/open', json={'path': capture})
                    route = '/api/projects/' + project['id']
                    annotations = project['annotations']
                    annotations['scene'] = [{'id': 'technical-scene', 'label': '室内', 'start_ms': 0,
                                              'end_ms': project['duration_ms'], 'mode': 'state'}]
                    saved = target.request('PUT', route + '/draft', json={'annotations': annotations,
                                             'expected_revision': project['revision']})
                    target.request('POST', route + '/writeback', json={'expected_revision': saved['revision']})
                    inspection = target.request('POST', '/api/source-claims/inspect', json={'path': capture})
                    reopened = target.request('POST', '/api/storage/projects/open', json={'path': capture,
                        'claim_mode': 'continue', 'claim_token': inspection['claim_token']})
                    if reopened['annotations']['scene'][0]['label'] != '室内':
                        raise RuntimeError('R2 JSON roundtrip did not preserve the annotation')
                    checked(client.put(route + '/draft', json={'annotations': annotations,
                        'expected_revision': project['revision']}), 'stale draft rejection', 409)
                    print('PASS authenticated draft, versioned JSON writeback, reopen and stale revision rejection', flush=True)
                    pointer_key = capture + '/timeline/.latest.json'
                    pointer, old_etag = provider.optional_json(pointer_key)
                    concurrent = json.dumps({**pointer, 'technical_concurrent_write': True}).encode()
                    provider.put(pointer_key, concurrent, etag=old_etag)
                    try:
                        provider.put(pointer_key, b'stale writer', etag=old_etag)
                    except HTTPException as error:
                        if error.status_code != 409:
                            raise
                    else:
                        raise RuntimeError('R2 did not reject the stale object ETag')
                    checked(client.post(route + '/writeback', json={'expected_revision': reopened['revision']}),
                            'concurrent writeback rejection', 409)
                    if provider.get(pointer_key) != concurrent:
                        raise RuntimeError('concurrent R2 version was overwritten')
                    print('PASS actual R2 If-Match conflict and application writeback conflict protection', flush=True)
                    target.request('POST', route + '/session/prepare', json={})
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        state = target.request('GET', route + '/session')
                        if state['state'] in ('ready', 'partial', 'error'):
                            break
                        time.sleep(.1)
                    if state['state'] != 'ready':
                        raise RuntimeError('playback preparation did not become ready')
                    playback = target.request('GET', route + '/session/manifest')
                    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as download:
                        for asset in playback['assets']:
                            response = download.get(asset['url'], headers={'Origin': origin})
                            if (response.status_code != 200 or len(response.content) != asset['size'] or
                                    hashlib.sha256(response.content).hexdigest() != asset['sha256'] or
                                    response.headers.get('access-control-allow-origin') != origin):
                                raise RuntimeError('signed playback asset readback or CORS check failed')
                    normal = next(asset for asset in playback['assets'] if asset['url'] == playback['videos'][0]['url'])
                    retained = root / 'Desktop/dataMarkProject' / capture.split('/')[-1] / normal['file_name']
                    if not retained.is_file() or hashlib.sha256(retained.read_bytes()).hexdigest() != normal['sha256']:
                        raise RuntimeError('uploader desktop cache differs from the downloaded normal video')
                    print('PASS normal/fast videos, cover/storyboards, signed downloads, CORS and desktop cache identity', flush=True)
                    remote_job = client.app.state.r2_uploads.start(login['user'], {
                        'prefix': 'daily/20261015/permission_verification', 'collector_name': 'technical',
                        'uploader_name': 'technical', 'files': [{'key': 'FPV/' + video.stem + '.mp4',
                        'size': normal['size'], 'sha256': normal['sha256'], 'duration_ms': project['duration_ms']}]})
                    auth.set_upload_permission(worker['id'], False)
                    checked(client.get('/api/r2/uploads/' + remote_job['id'] + '/files/0'), 'upload revocation', 403)
                    other = checked(client.post('/api/auth/login', json={'username': 'other', 'password': password}), 'other fixture login')
                    client.headers['X-CSRF-Token'] = other['csrf']
                    checked(client.get(route), 'cross-account project rejection', 403)
                    print('PASS fresh permission revocation and cross-account project isolation', flush=True)
                    print(json.dumps({'result': 'passed', 'test_object_prefix': prefix,
                        'asset_count': len(playback['assets']), 'production_accounts_used': False,
                        'production_services_changed': False, 'real_user_acceptance': False}), flush=True)
        finally:
            uploader.sessions.close()
            uploader.previews.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--confirm-test-writes', action='store_true', required=True)
    args = parser.parse_args()
    verify(args.config)


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    except HTTPException as error:
        raise SystemExit(f'Live R2 verification failed: HTTP {error.status_code}: {error.detail}') from None
    except Exception as error:
        # HTTP client exceptions may include signed URLs: never print them.
        raise SystemExit('Live R2 verification failed: ' + type(error).__name__) from None
