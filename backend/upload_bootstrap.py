"""Runs with the project-managed Python created by the credential-free installer."""
import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from pathlib import Path


def download(url):
    request=urllib.request.Request(url,headers={'User-Agent':'DataMark-Uploader/1.0'})
    with urllib.request.urlopen(request,timeout=120) as response:
        return response.read()


def unpack_source(payload,target):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for item in archive.infolist():
            parts=item.filename.split('/')
            if (any(part in {'','.', '..'} for part in parts) or '\\' in item.filename
                    or ':' in item.filename or stat.S_ISLNK(item.external_attr>>16)
                    or not (parts[0]=='backend' or item.filename in {'requirements.lock.txt','requirements.uploader.lock.txt'})):
                raise RuntimeError('安装包包含非法路径，已停止。')
        archive.extractall(target)


def tools(root,source):
    from importlib.util import spec_from_file_location,module_from_spec
    spec=spec_from_file_location('upload_install',source/'backend/upload_install.py')
    config=module_from_spec(spec); spec.loader.exec_module(config)
    destination=root/'.tools/ffmpeg/bin'
    destination.mkdir(parents=True,exist_ok=True)
    if sys.platform=='darwin':
        arch='arm64' if platform.machine().lower() in {'arm64','aarch64'} else 'amd64'
        timestamp,hashes=config.MAC_TOOLS[arch]
        packages=[(name,f'https://ffmpeg.martin-riedl.de/download/macos/{arch}/{timestamp}_9.0.2/{name}.zip',checksum) for name,checksum in hashes.items()]
    elif sys.platform=='win32' and platform.machine().lower() in {'amd64','x86_64'}:
        packages=[('windows',*config.WINDOWS_TOOLS)]
    else:
        raise RuntimeError('目前支持 macOS Intel／Apple Silicon 和 Windows x64。')
    for name,url,checksum in packages:
        cached=root/'.cache'/('ffmpeg-'+checksum+'.zip')
        cached.parent.mkdir(exist_ok=True)
        payload=cached.read_bytes() if cached.exists() else b''
        if hashlib.sha256(payload).hexdigest()!=checksum:
            payload=download(url)
        if hashlib.sha256(payload).hexdigest()!=checksum:
            raise RuntimeError('压缩工具下载校验失败，未安装。请重新运行安装命令。')
        cached.write_bytes(payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for tool in ('ffmpeg','ffprobe') if name=='windows' else (name,):
                filename=tool+('.exe' if sys.platform=='win32' else '')
                matches=[entry for entry in archive.infolist() if Path(entry.filename).name==filename and not entry.is_dir()]
                if len(matches)!=1:
                    raise RuntimeError('压缩工具安装包结构无效。')
                path=destination/filename
                path.write_bytes(archive.read(matches[0]))
                if sys.platform!='win32':path.chmod(0o755)
    return destination


def offline_kit(directory):
    directory=directory.resolve()
    ledger=json.loads((directory/'files.json').read_bytes())
    if ledger.get('target') not in {'macos-arm64','macos-x64','windows-x64'} or not isinstance(ledger.get('files'),dict):
        raise RuntimeError('离线安装包清单无效。')
    expected='windows-x64' if sys.platform=='win32' else 'macos-arm64' if platform.machine().lower() in {'arm64','aarch64'} else 'macos-x64'
    if ledger['target']!=expected:
        raise RuntimeError('安装包与当前电脑系统不符。')
    for name,checksum in ledger['files'].items():
        path=directory/name
        if (Path(name).is_absolute() or '..' in Path(name).parts or '\\' in name or ':' in name
                or not path.resolve().is_relative_to(directory) or path.is_symlink() or not path.is_file()):
            raise RuntimeError('离线安装包包含非法或缺失文件。')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=checksum:
                raise RuntimeError('离线安装包文件校验失败，请重新下载。')
    actual={p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
    if actual!=set(ledger['files'])|{'files.json'}:
        raise RuntimeError('离线安装包文件清单不完整或含额外文件。')
    return directory


def install_tools(kit,root):
    destination=root/'.tools/ffmpeg/bin'
    destination.mkdir(parents=True,exist_ok=True)
    for tool in ('ffmpeg','ffprobe'):
        filename=tool+('.exe' if sys.platform=='win32' else '')
        shutil.copyfile(kit/'tools'/filename,destination/filename)
        if sys.platform!='win32':(destination/filename).chmod(0o755)
    return destination


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--api',default='https://api-annotate.reltydynamic.com:8443')
    parser.add_argument('--frontend',default='https://datamark.reltydynamic.com')
    parser.add_argument('--browser',choices=['','chrome','edge','firefox','safari'],default='')
    parser.add_argument('--offline-dir',type=Path)
    parser.add_argument('--check-only',action='store_true',help='Install and verify without opening the browser or connecting to the platform')
    args=parser.parse_args()
    if not args.api.startswith('https://') or not args.frontend.startswith('https://'):
        raise RuntimeError('安装需要受信任的 HTTPS 平台地址。')
    root=args.root.resolve()
    root.mkdir(parents=True,exist_ok=True)
    (root/'.tmp').mkdir(exist_ok=True)
    env=os.environ.copy()
    env.update(PYTHONNOUSERSITE='1',PIP_REQUIRE_VIRTUALENV='1',PIP_CACHE_DIR=str(root/'.cache/pip'),
        TMP=str(root/'.tmp'),TEMP=str(root/'.tmp'),DATAMARK_UPLOADER_HOME=str(root))
    kit=offline_kit(args.offline_dir) if args.offline_dir else None
    print('正在校验并安装 DataMark，所有依赖均使用安装包内文件…' if kit else '正在下载 DataMark 源码…',flush=True)
    if kit:
        payload=(kit/'source.zip').read_bytes()
    else:
        info=json.loads(download(args.api+'/api/r2/uploader/bundle-info'))
        payload=download(args.api+'/api/r2/uploader/bundle.zip')
        if len(payload)!=info['size'] or hashlib.sha256(payload).hexdigest()!=info['sha256']:
            raise RuntimeError('安装包校验失败，请重新执行安装命令。')
    source=root/'.tmp'/('source-'+uuid.uuid4().hex)
    source.mkdir(parents=True)
    unpack_source(payload,source)
    install=[sys.executable,'-m','pip','install','--only-binary=:all:','--timeout','30','--retries','2']
    install+=['--no-index','--find-links',str(kit/'wheels')] if kit else ['--index-url','https://pypi.tuna.tsinghua.edu.cn/simple']
    subprocess.run([*install,'--requirement',str(source/('requirements.uploader.lock.txt' if kit else 'requirements.lock.txt'))],env=env,check=True)
    subprocess.run([sys.executable,'-m','pip','check'],env=env,check=True)
    tool_directory=install_tools(kit,root) if kit else tools(root,source)
    for name in ('ffmpeg','ffprobe'):
        filename=name+('.exe' if sys.platform=='win32' else '')
        env[name.upper()+'_PATH']=str(tool_directory/filename)
        subprocess.run([str(tool_directory/filename),'-version'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    # Install only into this dedicated project. Previous source is kept for
    # recovery; .venv, cache and local project state remain outside the source.
    installed=root/'app'
    if installed.exists():installed.rename(root/'.tmp'/('previous-'+uuid.uuid4().hex))
    source.rename(installed)
    (root/'README.txt').write_text('DataMark 本机上传组件。源码、Python、依赖和工具仅保存在此目录。\n压缩视频缓存保存在桌面 dataMarkProject。\n关闭组件后，可重新执行网页上的安装／启动命令。\nFFmpeg 来源及校验值见 app/backend/upload_install.py。\n',encoding='utf-8')
    if args.check_only:
        subprocess.run([sys.executable,'-c','import fastapi,httpx,websockets; import backend.upload_agent,backend.upload_pipeline,backend.prediction_prepare'],cwd=installed,env=env,check=True)
        print('DataMark 离线依赖和压缩工具已验证。',flush=True)
        return
    subprocess.run([sys.executable,'-m','backend.upload_agent','--api',args.api,'--frontend',args.frontend,'--browser',args.browser],cwd=installed,env=env,check=True)


if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,ValueError,subprocess.CalledProcessError) as error:
        raise SystemExit(str(error) if isinstance(error,RuntimeError) else '安装或启动未完成。请检查终端错误和网络，然后重新运行网页上的命令。') from None
