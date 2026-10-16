"""Outbound-only desktop compression component controlled by the DataMark page."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import HTTPException

from .native_picker import choose_local_paths
from .public_setup import public_origin
from .r2_folder import FolderUploadManager
from .r2_upload import RemoteCaptureTarget
from .service import ProjectService


class AgentTarget(RemoteCaptureTarget):
    def response(self, method, path, **kwargs):
        if path == '/api/auth/me':
            path = '/api/r2/helper/identity'
        elif path.startswith('/api/prediction/'):
            path = path.replace('/api/prediction/','/api/r2/helper/prediction/',1)
        elif path.startswith('/api/r2/uploads'):
            path = path.replace('/api/r2/uploads','/api/r2/helper/transfer/uploads',1)
        else:
            raise RuntimeError('本机组件不允许访问此接口。')
        return super().response(method,path,**kwargs)


class AgentEngine:
    def __init__(self, root, client, service=None, picker=choose_local_paths):
        self.root, self.client, self.picker = Path(root), client, picker
        self.service = service or ProjectService(self.root/'.local/r2-uploader',enable_remote=False)
        if service is None:
            self.service.desktop_cache.enabled = True
            tools = SimpleNamespace(root=self.root)
            self.service.tool = lambda name: ProjectService.tool(tools,name)
        self.target = AgentTarget(client,self.service)
        self.manager = FolderUploadManager(self.target,self.service.local/'bridge-staging')
        self.lock = threading.RLock()
        self.report = {'busy':False}
        self.received = self.completed = None
        self.job = None

    def capabilities(self):
        for name in ('ffmpeg','ffprobe'):
            tool = self.service.tool(name)
            if not tool:
                raise RuntimeError('缺少压缩工具，请重新执行网页显示的安装命令。')
            self.service.command([str(tool),'-version'])
        help_text = self.service.command([str(self.service.tool('ffmpeg')),'-hide_banner','-h','filter=scale']).stdout
        if 'reset_sar' not in help_text:
            raise RuntimeError('压缩工具版本过旧，请执行网页显示的安装命令更新。')

    def snapshot(self):
        with self.lock:
            result = dict(self.report)
            if self.job:
                result['job'] = self.manager.status(self.job['id'],self.job['owner'])
            return result

    def accept(self, command):
        with self.lock:
            if not command or command['id']==self.received:
                return
            if self.report['busy']:
                return
            self.received = command['id']
            self.completed = None
            self.report.update(busy=True,error='')
        threading.Thread(target=self.execute,args=(command,),daemon=True).start()

    def execute(self, command):
        try:
            user = self.target.request('GET','/api/auth/me')
            if not user.get('can_upload'):
                raise HTTPException(403,'上传权限已关闭。')
            if command['action']=='select':
                folders = self.picker(self.root,'directory')
                if folders:
                    selection = self.manager.select_folder(user['id'],folders[0])
                    self.target.attachments = selection['attachments']
                    self.selection = selection
                    self.job = None
                    with self.lock:
                        self.report['selection'] = {'name':selection['name'],'capture_date':selection['capture_date'],
                            'videos':[item['name'] for item in selection['videos']],
                            'attachments':[item['output'] for item in selection['attachments']], 'ignored':selection['ignored']}
            elif command['action']=='start':
                if not getattr(self,'selection',None):
                    raise HTTPException(422,'请先在网页选择本机采集文件夹。')
                fields = command.get('fields',{})
                job = self.manager.start(user,fields.get('collector',''),fields.get('uploader'),fields.get('note'),self.selection['selection_id'], prediction=fields)
                self.job = {**job,'owner':user['id']}
                def progress(detail):
                    with self.manager.lock:
                        self.manager.jobs[job['id']]['detail'] = detail
                self.target.progress = progress
                self.manager.finish(job['id'],user['id'],self.service,
                    lambda ident:self.target.request('GET','/api/auth/me').get('can_upload') is True)
                with self.manager.lock:
                    self.manager.jobs[job['id']]['detail'] = 'S3 上传完成，压缩缓存已保留到桌面。'
            else:
                raise HTTPException(422,'不支持此上传操作。')
        except Exception as error:
            detail = error.detail if isinstance(error,HTTPException) and isinstance(error.detail,str) else '本机操作未完成，请检查网络、设备连接和压缩工具后重试。'
            with self.lock:
                self.report['error'] = detail
        finally:
            with self.lock:
                self.report['busy'] = False
                self.completed = command['id']

    def close(self):
        self.service.sessions.close()
        self.service.previews.close()


def open_handoff(url, browser):
    """Use only the browser selected by the originating page; never the default."""
    if not browser:
        return False
    try:
        if sys.platform == 'darwin':
            apps = {'chrome': 'Google Chrome', 'edge': 'Microsoft Edge',
                    'firefox': 'Firefox', 'safari': 'Safari'}
            subprocess.run(['open', '-a', apps[browser], url], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if sys.platform == 'win32':
            paths = {'chrome': 'Google/Chrome/Application/chrome.exe',
                     'edge': 'Microsoft/Edge/Application/msedge.exe',
                     'firefox': 'Mozilla Firefox/firefox.exe'}
            relative = paths.get(browser)
            if relative:
                for key in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'):
                    base = os.environ.get(key)
                    executable = Path(base) / relative if base else None
                    if executable and executable.is_file():
                        subprocess.Popen([str(executable), url])
                        return True
    except (OSError, subprocess.CalledProcessError, KeyError):
        pass
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api',default='https://api-annotate.reltydynamic.com:8443')
    parser.add_argument('--frontend',default='https://datamark.reltydynamic.com')
    parser.add_argument('--browser',choices=['','chrome','edge','firefox','safari'],default='')
    args = parser.parse_args()
    public_origin(args.api,api=True)
    public_origin(args.frontend)
    if sys.platform not in {'darwin','win32'}:
        raise SystemExit('本机上传组件仅用于 macOS／Windows。')
    os.environ['PYTHONNOUSERSITE']='1'
    root = Path(os.getenv('DATAMARK_UPLOADER_HOME') or Path(__file__).resolve().parents[1])
    with httpx.Client(base_url=args.api,timeout=30,headers={'Origin':args.api}) as client:
        engine = AgentEngine(root,client)
        try:
            engine.capabilities()
            health=client.get('/api/health'); health.raise_for_status()
            if 's3-helper-bridge' not in health.json().get('capabilities',[]):
                raise RuntimeError('公网平台尚未更新到同页上传版本。')
            registered=client.post('/api/r2/helper/register',json={'platform':sys.platform,'protocol':1})
            registered.raise_for_status()
            registered=registered.json()
            client.headers.update({'Authorization':'Bearer '+registered['token'],'X-Upload-Agent':registered['id']})
            # Single-use association stays out of shell history and server URLs.
            # The website removes the fragment before starting normal requests.
            handoff = args.frontend+'/#datamark-upload='+registered['id']+':'+registered['claim']
            if not open_handoff(handoff, args.browser):
                print('请在原来已登录的浏览器中打开以下一次性关联地址（请勿分享）：')
                print(handoff)
            print('本机组件已就绪，请回到 DataMark 原页面关联并上传。无需再次输入密码。')
            print('请保持此终端开启；按 Ctrl+C 停止组件。')
            while True:
                try:
                    response=client.post('/api/r2/helper/poll',json={'received':engine.received,'completed':engine.completed,'report':engine.snapshot()})
                    if response.status_code in {401,403}:
                        raise RuntimeError('组件关联已失效，请登录原网页后重新执行启动命令。')
                    response.raise_for_status()
                    engine.accept(response.json().get('command'))
                except httpx.HTTPError:
                    # Keep compressed cache and multipart state during a network
                    # interruption. Never print requests containing signed URLs.
                    pass
                time.sleep(1)
        finally:
            engine.close()


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (RuntimeError,HTTPException,httpx.HTTPError,OSError,ImportError) as error:
        detail = str(error) if isinstance(error,RuntimeError) else '本机组件未能启动，请检查网络或重新执行安装命令。'
        raise SystemExit(detail) from None
