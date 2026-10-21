"""Local AVI compression and direct R2 multipart transfer; no R2 keys on clients."""
import argparse
import getpass
import json
import os
from pathlib import Path
import time
import threading
from types import SimpleNamespace

import httpx
from fastapi import HTTPException

from .native_picker import choose_local_paths
from .public_setup import public_origin
from .service import ProjectService
from .r2_folder import FolderUploadManager, attachment_manifest
from .playback_contract import SINGLE_VIDEO_PROFILE, SINGLE_VIDEO_CAPABILITY


class RemoteCaptureTarget:
    cache_profile = SINGLE_VIDEO_PROFILE

    def __init__(self, client, service, *, attachments=(), progress=None):
        self.client, self.service = client, service
        self.attachments = attachments
        self.progress = progress or (lambda value: None)
        self.transfer_progress = lambda value: None
        self.pipeline_enabled = False
        self.single_video_ready = False
        self.hash_guard=threading.RLock()
        self.source_hashes={}
        self.source_hash_locks={}

    def source_digest(self, record):
        from .mock_s3 import MockUploadManager, file_sha256
        path=MockUploadManager._check_local_source(record)
        key=(str(path),record['size'],record['mtime_ns'],record['device'],record['inode'])
        with self.hash_guard:lock=self.source_hash_locks.setdefault(key,threading.RLock())
        with lock:
            if key not in self.source_hashes:
                value=file_sha256(path)
                MockUploadManager._check_local_source(record)
                self.source_hashes[key]=value
            return self.source_hashes[key]

    def response(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if not response.is_success:
            try:
                message = response.json().get('detail', '上传未完成。')
            except ValueError:
                message = '上传接口暂时不可用。'
            raise HTTPException(response.status_code, message if isinstance(message, str) else '上传接口暂时不可用。')
        return response

    def request(self, method, path, **kwargs):
        return self.response(method, path, **kwargs).json()

    def capture_exists(self, prefix):
        # The server performs the authoritative conditional-publication check.
        return False

    def require_upload_support(self):
        health = self.request('GET', '/api/health')
        capabilities = health.get('capabilities', [])
        if SINGLE_VIDEO_CAPABILITY not in capabilities:
            raise HTTPException(409, '平台尚不支持单份视频缓存，请先更新平台后台，再使用新版本机组件上传。')
        self.pipeline_enabled = 's3-upload-pipeline-v1' in capabilities
        self.single_video_ready = True

    def publish_capture(self, prefix, prepared, manifest, service):
        if not self.single_video_ready:
            self.require_upload_support()
        for item in manifest['files']:
            item['duration_ms'] = service.probe(prepared / item['key'])['duration_ms']
        from .upload_playback import build
        build(service, prepared, manifest, self.progress, profile=self.cache_profile)
        return self.publish_ready(prefix,prepared,manifest,service)

    def publish_ready(self, prefix, prepared, manifest, service, *, prepare=False, completed_keys=None):
        # Stable request metadata permits restart/resume without storing tokens.
        manifest.pop('created_at', None)
        if self.attachments:
            manifest['attachments'] = attachment_manifest(self.attachments, self.source_digest)
        job = self.request('POST', '/api/r2/uploads', json=manifest)
        from .upload_transfer import transfer_files
        transfer_files(self, job, prepared, manifest, completed_keys=completed_keys)
        self.progress('文件已传输，正在校验并发布采集目录，请稍候。')
        finish_url = f"/api/r2/uploads/{job['id']}/finish?background=1" + ('&prepare=1' if prepare else '')
        expected = 'verified' if prepare else 'ready'
        accepted={expected,'ready'} if prepare else {expected}
        try:
            result = self.request('POST', finish_url, timeout=600)
        except (httpx.TransportError, HTTPException) as error:
            if isinstance(error, HTTPException) and error.status_code not in {502, 503, 504}:
                raise
            # A lost response does not mean publication failed. Read durable
            # state through the same owner/permission checks before reporting.
            try:
                result = self.request('GET', f"/api/r2/uploads/{job['id']}")
            except (httpx.HTTPError, HTTPException):
                result = None
            if not result or result.get('state') not in accepted | {'validating'}:
                raise HTTPException(504, '文件已传输，但尚未确认采集目录发布结果。请稍后重试，已上传内容可续传，不会覆盖已有目录。') from None
        deadline = time.monotonic() + 7200
        while result.get('state') == 'validating' or (not prepare and result.get('state') == 'verified'):
            checked, total = result.get('verified_files', 0), result.get('file_count', 0)
            read_bytes, total_bytes = result.get('validation_bytes', 0), result.get('validation_total_bytes', 0)
            percent = read_bytes / total_bytes * 100 if total_bytes else checked / max(1,total) * 100
            self.transfer_progress(dict(state='validating', progress=round(percent,1),
                                        verified_files=checked, files_total=total))
            amount = f' · 已读取 {read_bytes / 1024**2:.1f}/{total_bytes / 1024**2:.1f} MiB' if total_bytes else ''
            self.progress(f'正在校验并发布：{checked}/{total} 个文件已通过{amount}。')
            if time.monotonic() >= deadline:
                raise HTTPException(504, '后台仍在核验，请稍后重试继续；已上传内容保留。')
            time.sleep(2)
            result = self.request('GET', f"/api/r2/uploads/{job['id']}", timeout=30)
            if result.get('state') in {'validating','verified'} and result.get('validation_active') is False:
                # Resume after an API restart; errors remain visible until an explicit retry.
                result = self.request('POST', finish_url, timeout=30)
        if result.get('state') == 'validation_error':
            raise HTTPException(422, result.get('error') or '采集目录核验失败，可重试。')
        if result.get('state') not in accepted:
            raise HTTPException(502, '服务端尚未确认采集目录发布成功，请稍后重试。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='https://api-annotate.reltydynamic.com:8443')
    parser.add_argument('--video-dir', type=Path)
    parser.add_argument('--collector')
    parser.add_argument('--username')
    parser.add_argument('--predict', action='store_true', help='Generate posture prediction before compression')
    parser.add_argument('--clock-group', help='Explicit continuous chest/camera clock group; omit after any restart or clock change')
    parser.add_argument('--chest-device')
    parser.add_argument('--thigh-device')
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
        with httpx.Client(base_url=args.api, timeout=180, trust_env=False) as client:
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
            target.pipeline_enabled = 's3-upload-pipeline-v1' in target.request('GET','/api/health').get('capabilities',[])
            manager = FolderUploadManager(target, local / 'staging')
            selection = manager.select_folder(user['id'], folders[0])
            target.attachments = selection['attachments']
            job = manager.start(user, collector, None, None, selection['selection_id'], prediction={
                'prediction': 'generate' if args.predict else 'keep', 'clock_group': args.clock_group or '',
                'chest_device': args.chest_device, 'thigh_device': args.thigh_device})
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
