"""Build complete, platform-specific uploader kits; clients need only DataMark HTTPS."""
import argparse
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import urllib.error
from urllib.parse import urlsplit, unquote
import zipfile

PYTHON_VERSION = '3.14.6'
PYTHON_RELEASE = '20260804'
TARGETS = {
    'macos-arm64': ('aarch64-apple-darwin', 'e0d4e5066aef3248c22e08a07546bfd5edeaca904c91fadc296be720998ec2ec'),
    'macos-x64': ('x86_64-apple-darwin', '78ded4c4f0b6a931240f09bd20608533bb40dd71c1ff953c7267359075ca8b16'),
    'windows-x64': ('x86_64-pc-windows-msvc', 'ada613a2af32c04cf7c54726243c56d76f0e59f5f61d2f28371d3b9d8efad566'),
}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def cached_download(url, checksum, cache):
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / checksum
    temporary = path.with_suffix('.part')
    label = unquote(Path(urlsplit(url).path).name)
    deadline = time.monotonic() + 900
    for attempt in range(3):
        # A completed cache can also be supplied while an earlier attempt failed.
        if path.is_file() and sha(path) == checksum:
            print('复用已核验依赖：' + label, flush=True)
            return path
        if temporary.is_file() and sha(temporary) == checksum:
            os.replace(temporary, path)
            print('复用已完成下载：' + label, flush=True)
            return path
        offset = temporary.stat().st_size if temporary.is_file() else 0
        print(f'下载 {label}：第 {attempt+1}/3 次，从 {offset/1024**2:.1f} MiB 继续', flush=True)
        headers = {'User-Agent': 'DataMark-Packager/1.0'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError('依赖下载超过 15 分钟，已保留断点，请检查构建机网络后重试。')
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as source:
                status = getattr(source, 'status', 200)
                response_headers = getattr(source, 'headers', {})
                if status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response_headers.get('Content-Range', ''))
                    if not match or int(match[1]) != offset or not offset <= int(match[2]) < int(match[3]):
                        raise OSError('依赖断点响应范围无效。')
                    total = int(match[3])
                elif status == 200:
                    offset = 0  # An origin that ignores Range must replace, never append.
                    total = int(response_headers.get('Content-Length', 0))
                else:
                    raise OSError('依赖下载响应状态无效。')
                received = offset
                started = last = time.monotonic()
                with temporary.open('ab' if offset else 'wb') as output:
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('依赖下载超过 15 分钟，已保留断点，请检查构建机网络后重试。')
                        block = source.read(64*1024)
                        if not block:
                            break
                        output.write(block)
                        received += len(block)
                        now = time.monotonic()
                        if now-last >= 10:
                            size = f'{total/1024**2:.1f}' if total else '?'
                            rate = (received-offset)/max(.001, now-started)/1024
                            print(f'下载 {label}：{received/1024**2:.1f}/{size} MiB，{rate:.1f} KiB/s', flush=True)
                            last = now
                if total and received != total:
                    raise OSError('依赖下载未完整结束，保留断点。')
            if sha(temporary) != checksum:
                temporary.unlink(missing_ok=True)
                raise RuntimeError('官方依赖包摘要不一致，停止构建。')
            os.replace(temporary, path)
            print(f'下载并核验完成：{label}（{received/1024**2:.1f} MiB）', flush=True)
            return path
        except OSError as error:
            # A stale/oversized partial can produce 416; restart safely next time.
            invalid_partial=isinstance(error, urllib.error.HTTPError) and error.code == 416
            if invalid_partial:
                temporary.unlink(missing_ok=True)
            reason = ('HTTP '+str(error.code)) if isinstance(error, urllib.error.HTTPError) else type(error).__name__
            recovery='断点无效，下次从头下载' if invalid_partial else '已完成部分保留供重试'
            print(f'依赖下载未完成：{label}，{reason}；{recovery}。', flush=True)
            if attempt == 2 or time.monotonic() >= deadline:
                raise


def python_archive(target, cache):
    triplet, checksum = TARGETS[target]
    filename = f'cpython-{PYTHON_VERSION}%2B{PYTHON_RELEASE}-{triplet}-install_only_stripped.tar.gz'
    path = cached_download('https://github.com/astral-sh/python-build-standalone/releases/download/' + PYTHON_RELEASE + '/' + filename, checksum, cache)
    # Validate all paths and links before a client ever extracts this archive.
    with tarfile.open(path) as archive:
        for item in archive.getmembers():
            parts = Path(item.name).parts
            if not parts or parts[0] != 'python' or '..' in parts or item.isdev() or item.isfifo():
                raise RuntimeError('Python 包包含非法路径或设备。')
            if item.issym() or item.islnk():
                from posixpath import normpath, join
                destination = normpath(join(str(Path(item.name).parent), item.linkname) if item.issym() else item.linkname)
                if destination != 'python' and not destination.startswith('python/'):
                    raise RuntimeError('Python 包包含越界链接。')
    return path


def wheel_platforms(target):
    if target == 'windows-x64':
        return ['win_amd64']
    from pip._vendor.packaging.tags import mac_platforms
    return list(mac_platforms(version=(11, 0), arch='arm64' if target == 'macos-arm64' else 'x86_64'))


def kit_key(root):
    # A source or dependency change requires a fresh kit; old content-addressed kits remain valid.
    from .upload_install import bundle
    with zipfile.ZipFile(io.BytesIO(bundle(root))) as archive:
        files=sorted((item.filename,hashlib.sha256(archive.read(item)).hexdigest()) for item in archive.infolist())
    # ZIP platform attributes differ on Windows; compare file contents, not archive metadata.
    return hashlib.sha256(json.dumps(files,separators=(',',':')).encode()+Path(__file__).read_bytes()).hexdigest()


def build(root, output, target, index):
    from .upload_install import bundle, MAC_TOOLS, WINDOWS_TOOLS
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    key = kit_key(root)
    marker = output / (target + '.json')
    if marker.is_file():
        previous = json.loads(marker.read_bytes())
        path = output / previous['file']
        if previous.get('source_key') == key and path.is_file() and sha(path) == previous['sha256']:
            print(target + ': 已复用完整安装包', flush=True)
            return previous
    cache = output / '.downloads'
    print(target + ': 准备 Python、压缩工具和离线依赖', flush=True)
    runtime = python_archive(target, cache)
    with tempfile.TemporaryDirectory(dir=output, prefix='.build-') as raw:
        work = Path(raw)
        shutil.copyfile(runtime, work / 'python.tar.gz')
        (work / 'source.zip').write_bytes(bundle(root))
        shutil.copyfile(root / 'backend/upload_bootstrap.py', work / 'bootstrap.py')
        wheels = cache / ('wheels-'+target+'-'+hashlib.sha256(b'uploader-only-v1'+(root/'requirements.uploader.lock.txt').read_bytes()).hexdigest())
        args = [sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--python-version', '3.14',
                '--implementation', 'cp', '--abi', 'cp314', '--abi', 'abi3', '--abi', 'none',
                '--index-url', index, '--dest', str(wheels), '-r', str(root / 'requirements.uploader.lock.txt')]
        for platform in wheel_platforms(target):
            args += ['--platform', platform]
        wheel_marker=wheels.with_suffix('.json')
        try:
            saved=json.loads(wheel_marker.read_bytes())
            wheel_ready=bool(saved) and set(saved)=={p.name for p in wheels.iterdir()} and all(
                name.endswith('.whl') and (wheels/name).is_file() and sha(wheels/name)==checksum for name,checksum in saved.items())
        except (OSError,ValueError,TypeError):
            wheel_ready=False
        if not wheel_ready:
            subprocess.run(args, check=True)
            saved={p.name:sha(p) for p in wheels.iterdir() if p.is_file()}
            if not saved or any(not name.endswith('.whl') for name in saved):
                raise RuntimeError('离线依赖包不完整。')
            wheel_marker.write_text(json.dumps(saved,sort_keys=True))
        shutil.copytree(wheels,work/'wheels')
        tools = work / 'tools'; tools.mkdir()
        if target.startswith('macos-'):
            arch = 'arm64' if target.endswith('arm64') else 'amd64'
            timestamp, hashes = MAC_TOOLS[arch]
            packages = [(name, f'https://ffmpeg.martin-riedl.de/download/macos/{arch}/{timestamp}_9.0.2/{name}.zip', checksum)
                        for name, checksum in hashes.items()]
        else:
            packages = [('windows', *WINDOWS_TOOLS)]
        provenance=['Python: https://github.com/astral-sh/python-build-standalone (license files are inside python.tar.gz)']
        for name, url, checksum in packages:
            provenance.append(url+' SHA256 '+checksum)
            with zipfile.ZipFile(cached_download(url, checksum, cache)) as archive:
                for tool in ('ffmpeg', 'ffprobe') if name == 'windows' else (name,):
                    filename = tool + ('.exe' if name == 'windows' else '')
                    matches = [item for item in archive.infolist() if Path(item.filename).name == filename]
                    if len(matches) != 1:
                        raise RuntimeError('压缩工具包结构无效。')
                    (tools / filename).write_bytes(archive.read(matches[0]))
                for entry in archive.infolist():
                    basename=Path(entry.filename).name
                    if not entry.is_dir() and any(word in basename.lower() for word in ('license','copying','readme')):
                        (work/('FFmpeg-'+name+'-'+basename)).write_bytes(archive.read(entry))
        (work/'DEPENDENCIES.txt').write_text('\n'.join(provenance)+'\nFFmpeg source and build information: https://www.gyan.dev/ffmpeg/builds/ ; https://ffmpeg.martin-riedl.de/ ; https://ffmpeg.org/download.html\n',encoding='utf-8')
        from .upload_install import offline_shell, offline_powershell
        (work / 'install.command').write_text(offline_shell(), encoding='utf-8')
        (work / 'install.ps1').write_text(offline_powershell(), encoding='utf-8-sig')
        ledger = {p.relative_to(work).as_posix(): sha(p) for p in work.rglob('*') if p.is_file()}
        (work / 'files.json').write_text(json.dumps({'target': target, 'files': ledger}, sort_keys=True))
        archive_path = work / 'kit.zip'
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for name in [*ledger, 'files.json']:
                archive.write(work / name, name)
        checksum = sha(archive_path)
        name = target + '-' + checksum + '.zip'
        os.replace(archive_path, output / name)
        info = dict(target=target, source_key=key, file=name, sha256=checksum, size=(output/name).stat().st_size)
        temporary = marker.with_suffix('.tmp')
        temporary.write_text(json.dumps(info, sort_keys=True)); os.replace(temporary, marker)
        (output/(name+'.sha256')).write_text(checksum+'  '+name+'\n')
        print(target + ': 完成 ' + name, flush=True)
        return info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target', choices=list(TARGETS), action='append')
    parser.add_argument('--index-url', default='https://pypi.tuna.tsinghua.edu.cn/simple')
    args = parser.parse_args()
    for target in args.target or TARGETS:
        build(Path(__file__).resolve().parents[1], args.output, target, args.index_url)


if __name__ == '__main__':
    main()
