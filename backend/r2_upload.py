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
from .r2_folder import FolderUploadManager, attachment_manifest


class RemoteCaptureTarget:
    def __init__(self, client, service, *, attachments=(), progress=None):
        self.client, self.service = client, service
        self.attachments = attachments
        self.progress = progress or (lambda value: None)

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if not response.is_success:
            try:
                message = response.json().get('detail', '上传未完成。')
            except ValueError:
                message = '上传接口暂时不可用。'
            raise HTTPException(response.status_code, message if isinstance(message, str) else '上传接口暂时不可用。')
        return response.json()

    def capture_exists(self, prefix):
        # The server performs the authoritative conditional-publication check.
        return False

    def publish_capture(self, prefix, prepared, manifest, service):
        for item in manifest['files']:
            item['duration_ms'] = service.probe(prepared / item['key'])['duration_ms']
        # Stable request metadata permits restart/resume without storing tokens.
        manifest.pop('created_at', None)
        if self.attachments:
            manifest['attachments'] = attachment_manifest(self.attachments)
        items = manifest['files'] + manifest.get('attachments', [])
        job = self.request('POST', '/api/r2/uploads', json=manifest)
        for index, item in enumerate(items):
            record = self.attachments[index - len(manifest['files'])] if index >= len(manifest['files']) else None
            source = MockUploadManager._check_local_source(record) if record else prepared / item['key']
            self.progress(f"正在上传 {index + 1}/{len(items)}：{item['key']}")
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
            if record:
                MockUploadManager._check_local_source(record)
        self.progress('文件已传输，正在校验并发布采集目录，请稍候。')
        try:
            result = self.request('POST', f"/api/r2/uploads/{job['id']}/finish", timeout=600)
        except (httpx.TransportError, HTTPException) as error:
            if isinstance(error, HTTPException) and error.status_code not in {502, 503, 504}:
                raise
            # A lost response does not mean publication failed. Read durable
            # state through the same owner/permission checks before reporting.
            try:
                result = self.request('GET', f"/api/r2/uploads/{job['id']}")
            except (httpx.HTTPError, HTTPException):
                result = None
            if not result or result.get('state') != 'ready':
                raise HTTPException(504, '文件已传输，但尚未确认采集目录发布结果。请稍后重试，已上传内容可续传，不会覆盖已有目录。') from None
        if result.get('state') != 'ready':
            raise HTTPException(502, '服务端尚未确认采集目录发布成功，请稍后重试。')


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
        folders = [str(args.video_dir)] if args.video_dir else choose_local_paths(root, 'directory')
        if not folders:
            raise RuntimeError('没有选择采集文件夹。')
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
            manager = FolderUploadManager(target, local / 'staging')
            selection = manager.select_folder(user['id'], folders[0])
            target.attachments = selection['attachments']
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
