"""Local AVI compression and direct R2 multipart transfer; no R2 keys on clients."""
import argparse
import getpass
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
from fastapi import HTTPException

from .mock_s3 import MockUploadManager
from .native_picker import choose_local_paths
from .public_setup import public_origin
from .service import ProjectService


class RemoteCaptureTarget:
    def __init__(self, client, service):
        self.client, self.service = client, service

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if not response.is_success:
            try:
                message = response.json().get('detail', '上传未完成。')
            except ValueError:
                message = '上传接口暂时不可用。'
            raise RuntimeError(message)
        return response.json()

    def capture_exists(self, prefix):
        # The server performs the authoritative conditional-publication check.
        return False

    def publish_capture(self, prefix, prepared, manifest, service):
        for item in manifest['files']:
            item['duration_ms'] = service.probe(prepared / item['key'])['duration_ms']
        # Stable request metadata permits restart/resume without storing tokens.
        manifest.pop('created_at', None)
        job = self.request('POST', '/api/r2/uploads', json=manifest)
        for index, item in enumerate(manifest['files']):
            source = prepared / item['key']
            for attempt in range(3):
                plan = self.request('GET', f"/api/r2/uploads/{job['id']}/files/{index}")
                if plan['complete']:
                    break
                try:
                    with source.open('rb') as stream, httpx.Client(timeout=180, follow_redirects=False) as direct:
                        for part in plan['parts']:
                            if part['uploaded']:
                                continue
                            stream.seek((part['number'] - 1) * plan['part_size'])
                            payload = stream.read(plan['part_size'])
                            response = direct.put(part['url'], content=payload)
                            if not response.is_success:
                                raise RuntimeError('R2 分片上传未确认成功。')
                    self.request('POST', f"/api/r2/uploads/{job['id']}/files/{index}/complete")
                    break
                except (httpx.HTTPError, RuntimeError):
                    if attempt == 2:
                        raise RuntimeError('分片上传中断，可重新运行上传工具续传。') from None
                    time.sleep(1)
        self.request('POST', f"/api/r2/uploads/{job['id']}/finish")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='https://api-annotate.reltydynamic.com:8443')
    parser.add_argument('--video-dir', type=Path)
    parser.add_argument('--collector')
    parser.add_argument('--username')
    args = parser.parse_args()
    public_origin(args.api, api=True)
    root = Path(__file__).resolve().parents[1]
    os.environ.update(PYTHONNOUSERSITE='1', DATAMARK_DESKTOP_CACHE='1')
    local = root / '.local/r2-uploader'
    local.mkdir(parents=True, exist_ok=True)
    service = ProjectService(local, enable_remote=False)
    # Project tools are reused without installing global FFmpeg.
    tools = SimpleNamespace(root=root)
    service.tool = lambda name: ProjectService.tool(tools, name)
    try:
        paths = ([str(path) for path in args.video_dir.iterdir() if path.is_file() and path.suffix.lower() == '.avi'] if args.video_dir
                 else choose_local_paths(root, 'capture_avi'))
        if not paths:
            raise RuntimeError('没有选择 AVI 视频。')
        username = args.username or input('内网/公网通用账号：').strip()
        password = getpass.getpass('密码（不回显、不保存）：')
        collector = args.collector or input('采集人姓名：').strip()
        with httpx.Client(base_url=args.api, timeout=180, trust_env=True) as client:
            response = client.post('/api/auth/login', json={'username': username, 'password': password}, headers={'Origin': args.api})
            password = None
            if response.status_code != 200:
                raise RuntimeError('账号登录失败，请核对账号、密码和网络。')
            data = response.json()
            user = data['user']
            client.headers.update({'X-CSRF-Token': data['csrf'], 'Origin': args.api})
            if not user.get('can_upload'):
                raise RuntimeError('此账号尚未获得上传权限。')
            target = RemoteCaptureTarget(client, service)
            manager = MockUploadManager(target, local / 'staging')
            selection = manager.select_local(user['id'], paths)
            job = manager.start(user, collector, None, None, selection['selection_id'])
            def permitted(ident):
                return target.request('GET', '/api/auth/me').get('can_upload') is True
            manager.finish(job['id'], user['id'], service, permitted)
            print('缓存已上传到 R2，并保留在桌面 dataMarkProject 对应采集目录。')
            print('采集目录：' + job['prefix'])
            target.request('POST', '/api/auth/logout')
    finally:
        service.sessions.close()
        service.previews.close()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, httpx.HTTPError, HTTPException, OSError):
        raise SystemExit('上传未完成。请检查账号、网络及视频；已生成的桌面缓存和服务端分片可保留用于重试。') from None
