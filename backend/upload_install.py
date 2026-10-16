"""Public, credential-free installers; every runtime stays in one project folder."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response, FileResponse

MAC_TOOLS = {
 'arm64': ('1789931890', {'ffmpeg':'c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924','ffprobe':'fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6'}),
 'amd64': ('1789931006', {'ffmpeg':'7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4','ffprobe':'2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd'})}

WINDOWS_TOOLS = ('https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/ffmpeg-9.0.1-essentials_build.zip',
                 'fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9')


def bundle(root):
    buffer=io.BytesIO()
    root=Path(root)
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((root/'backend').rglob('*')):
            if (path.is_file() and not path.is_symlink() and not path.name.startswith('test_')
                    and not path.name.endswith('.test.mjs') and '__pycache__' not in path.parts
                    and not any(part.startswith('.') for part in path.relative_to(root).parts)
                    and path.suffix in {'.py','.html','.css','.js','.mjs'}):
                archive.writestr(zipfile.ZipInfo(path.relative_to(root).as_posix(), (2020,1,1,0,0,0)),path.read_bytes())
        for name in ('requirements.lock.txt','requirements.uploader.lock.txt'):
            if (root/name).is_file():
                archive.writestr(zipfile.ZipInfo(name, (2020,1,1,0,0,0)),(root/name).read_bytes())
    return buffer.getvalue()


def browser_argument(browser):
    if browser not in {'', 'chrome', 'edge', 'firefox', 'safari'}:
        raise HTTPException(422, '浏览器参数无效。')
    return " --browser '" + browser + "'" if browser else ''


def offline_shell():
    return r'''#!/bin/sh
set -eu
kit=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root="$HOME/Desktop/DataMarkUploader"
mkdir -p "$root/.tools" "$root/.tmp" "$root/.cache/pip"
export PYTHONNOUSERSITE=1 PIP_REQUIRE_VIRTUALENV=1
export PIP_CACHE_DIR="$root/.cache/pip" TMPDIR="$root/.tmp"
runtime_hash=$(shasum -a 256 "$kit/python.tar.gz" | cut -d ' ' -f 1)
case "$(uname -m):$runtime_hash" in
 arm64:e0d4e5066aef3248c22e08a07546bfd5edeaca904c91fadc296be720998ec2ec|x86_64:78ded4c4f0b6a931240f09bd20608533bb40dd71c1ff953c7267359075ca8b16) ;;
 *) echo 'Python 安装包校验失败或系统不符。'; exit 1 ;;
esac
runtime="$root/.tools/python-$runtime_hash"
if [ ! -x "$runtime/python/bin/python3" ]; then
 echo '正在安装项目专用 Python…'
 stage=$(mktemp -d "$root/.tmp/python.XXXXXX")
 tar -xzf "$kit/python.tar.gz" -C "$stage"
 mv "$stage" "$runtime"
fi
if [ ! -x "$root/.venv/bin/python" ]; then
 "$runtime/python/bin/python3" -m venv "$root/.venv"
fi
exec "$root/.venv/bin/python" "$kit/bootstrap.py" --root "$root" --offline-dir "$kit" "$@"
'''


def offline_powershell():
    return r'''param([string]$api='https://api-annotate.reltydynamic.com:8443', [string]$frontend='https://datamark.reltydynamic.com', [string]$browser='')
$ErrorActionPreference = 'Stop'
$kit = $DataMarkKit
if (-not $kit) { $kit = $PSScriptRoot }
$root = Join-Path ([Environment]::GetFolderPath('Desktop')) 'DataMarkUploader'
$names = @('PYTHONNOUSERSITE','PIP_REQUIRE_VIRTUALENV','PIP_CACHE_DIR','TEMP','TMP')
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name,'Process') }
try {
 foreach ($folder in @('.tools','.cache/pip','.tmp')) { New-Item -ItemType Directory -Force (Join-Path $root $folder) | Out-Null }
 $env:PYTHONNOUSERSITE='1'; $env:PIP_REQUIRE_VIRTUALENV='1'; $env:PIP_CACHE_DIR=Join-Path $root '.cache/pip'
 $env:TEMP=Join-Path $root '.tmp'; $env:TMP=$env:TEMP
 $runtimeHash=(Get-FileHash (Join-Path $kit 'python.tar.gz') -Algorithm SHA256).Hash.ToLowerInvariant()
 if ($runtimeHash -ne 'ada613a2af32c04cf7c54726243c56d76f0e59f5f61d2f28371d3b9d8efad566') { throw 'Python package checksum mismatch' }
 $runtime=Join-Path $root ('.tools/python-'+$runtimeHash)
 $base=Join-Path $runtime 'python/python.exe'
 if (-not (Test-Path $base)) {
  Write-Host '正在安装项目专用 Python…'
  $stage=Join-Path $root ('.tmp/python-'+[guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $stage | Out-Null
  & tar -xzf (Join-Path $kit 'python.tar.gz') -C $stage
  if ($LASTEXITCODE -ne 0) { throw 'Python extraction failed' }
  Move-Item $stage $runtime
 }
 $python=Join-Path $root '.venv/Scripts/python.exe'
 if (-not (Test-Path $python)) { & $base -m venv (Join-Path $root '.venv'); if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed' } }
 & $python (Join-Path $kit 'bootstrap.py') --root $root --offline-dir $kit --api $api --frontend $frontend --browser $browser
 if ($LASTEXITCODE -ne 0) { throw 'DataMark installation or startup failed' }
} finally { foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name,$saved[$name],'Process') } }
'''


def shell(api,frontend,browser='',kits=None):
    argument=browser_argument(browser)
    choices='\n'.join(f" {arch}) name='{kits[target]['file']}'; checksum='{kits[target]['sha256']}'; size='{kits[target]['size']}' ;;" for arch,target in [('arm64','macos-arm64'),('x86_64','macos-x64')] if kits and target in kits)
    return f'''#!/bin/sh
set -eu
[ "$(uname -s)" = Darwin ] || {{ echo '此命令仅支持 macOS。'; exit 1; }}
case "$(uname -m)" in
{choices}
 *) echo '当前系统的完整安装包尚未准备好，请联系管理员。'; exit 1 ;;
esac
root="$HOME/Desktop/DataMarkUploader"
mkdir -p "$root/.cache/kits" "$root/.tmp"
archive="$root/.cache/kits/$name"
if [ -f "$archive.part" ]; then
 if [ "$(shasum -a 256 "$archive.part" | cut -d ' ' -f 1)" = "$checksum" ]; then mv "$archive.part" "$archive";
 elif [ "$(wc -c < "$archive.part" | tr -d ' ')" -ge "$size" ]; then rm -f "$archive.part"; fi
fi
if [ ! -f "$archive" ] || [ "$(shasum -a 256 "$archive" | cut -d ' ' -f 1)" != "$checksum" ]; then
 echo '正在从 DataMark 下载完整安装包，无需 VPN…'
 curl --fail --location --show-error --retry 3 --continue-at - '{api}/api/r2/uploader/packages/'"$name" -o "$archive.part"
 if [ "$(shasum -a 256 "$archive.part" | cut -d ' ' -f 1)" != "$checksum" ]; then rm -f "$archive.part"; echo '安装包校验失败，请重新运行命令。'; exit 1; fi
 mv "$archive.part" "$archive"
fi
kit="$root/.cache/kits/$checksum"
if [ ! -d "$kit" ]; then
 stage=$(mktemp -d "$root/.tmp/kit.XXXXXX")
 unzip -q "$archive" -d "$stage"
 mv "$stage" "$kit"
fi
exec sh "$kit/install.command" --api '{api}' --frontend '{frontend}'{argument}
'''


def powershell(api,frontend,browser='',kits=None):
    argument=browser_argument(browser)
    info=(kits or {}).get('windows-x64',{})
    return f'''$ErrorActionPreference = 'Stop'
if ([Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITEW6432') -eq 'ARM64' -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or -not [Environment]::Is64BitOperatingSystem) {{ throw '目前支持 Windows x64。' }}
$name='{info.get('file','')}'; $checksum='{info.get('sha256','')}'; $size={info.get('size',0)}
if (-not $name) {{ throw 'Windows 完整安装包尚未准备好，请联系管理员。' }}
$root=Join-Path ([Environment]::GetFolderPath('Desktop')) 'DataMarkUploader'
New-Item -ItemType Directory -Force (Join-Path $root '.cache/kits') | Out-Null
$archive=Join-Path $root ('.cache/kits/'+$name)
if (Test-Path ($archive+'.part')) {{
 if ((Get-FileHash ($archive+'.part') -Algorithm SHA256).Hash.ToLowerInvariant() -eq $checksum) {{ Move-Item -Force ($archive+'.part') $archive }}
 elseif ((Get-Item ($archive+'.part')).Length -ge $size) {{ Remove-Item ($archive+'.part') }}
}}
if (-not (Test-Path $archive) -or (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $checksum) {{
 Write-Host '正在从 DataMark 下载完整安装包，无需 VPN…'
 & curl.exe --fail --location --show-error --retry 3 --continue-at - ("{api}/api/r2/uploader/packages/$name") -o ($archive+'.part')
 if ($LASTEXITCODE -ne 0) {{ throw '安装包下载未完成，请再次运行同一命令继续。' }}
 if ((Get-FileHash ($archive+'.part') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $checksum) {{ Remove-Item ($archive+'.part'); throw '安装包校验失败，请重试。' }}
 Move-Item -Force ($archive+'.part') $archive
}}
$DataMarkKit=Join-Path $root ('.cache/kits/'+$checksum)
if (-not (Test-Path $DataMarkKit)) {{
 $stage=Join-Path $root ('.cache/kits/stage-'+[guid]::NewGuid().ToString('N'))
 Expand-Archive -LiteralPath $archive -DestinationPath $stage
 Move-Item $stage $DataMarkKit
}}
& ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $DataMarkKit 'install.ps1')))) -api '{api}' -frontend '{frontend}'{argument.replace('--browser', '-browser')}
'''


def install(app,root):
    from .public_setup import public_origin
    api=public_origin(os.environ['DATAMARK_PUBLIC_API_ORIGIN'],api=True)
    frontend=public_origin(os.environ['DATAMARK_PUBLIC_ORIGIN'])
    package=bundle(root)
    from .upload_dist import TARGETS, kit_key, sha
    distribution=Path(os.getenv('DATAMARK_UPLOADER_DIST',str(Path(root)/'uploader-dist')))
    expected=kit_key(root)
    app.state.uploader_source_key=expected
    kits={}
    for target in TARGETS:
        marker=distribution/(target+'.json')
        try:
            item=json.loads(marker.read_bytes())
            filename=target+'-'+item['sha256']+'.zip'
            path=distribution/filename
            if (not re.fullmatch('[a-f0-9]{64}',item['sha256']) or item['file']!=filename
                    or item['source_key']!=expected or path.is_symlink()
                    or path.stat().st_size!=item['size'] or sha(path)!=item['sha256']):
                continue
            kits[target]=item
        except (OSError,ValueError,KeyError,TypeError):
            continue

    @app.get('/api/r2/uploader/packages')
    def packages():
        return {'ready':len(kits)==len(TARGETS),'platforms':{target:{**item,'url':api+'/api/r2/uploader/packages/'+item['file']} for target,item in kits.items()}}

    @app.get('/api/r2/uploader/packages/{filename}')
    def package_file(filename:str):
        match=re.fullmatch(r'(macos-arm64|macos-x64|windows-x64)-[a-f0-9]{64}\.zip(\.sha256)?',filename)
        if not match or match[1] not in kits or filename.removesuffix('.sha256')!=kits[match[1]]['file']:
            raise HTTPException(404,'安装包不存在或已更新，请刷新页面。')
        item=kits[match[1]]
        if filename.endswith('.sha256'):
            return Response(item['sha256']+'  '+item['file']+'\n',media_type='text/plain')
        return FileResponse(distribution/filename,media_type='application/zip',filename=filename,
                            headers={'Cache-Control':'public, max-age=31536000, immutable'})

    @app.get('/api/r2/uploader/bundle-info')
    def info():
        return {'sha256':hashlib.sha256(package).hexdigest(),'size':len(package)}

    @app.get('/api/r2/uploader/bundle.zip')
    def archive():
        return Response(package,media_type='application/zip')

    @app.get('/api/r2/uploader/install.sh')
    def mac_installer(browser: str = ''):
        browser_argument(browser)
        if not all(target in kits for target in ('macos-arm64','macos-x64')):
            raise HTTPException(503,'macOS 完整安装包尚未就绪，请联系管理员。')
        return Response(shell(api,frontend,browser,kits),media_type='text/plain',headers={'Cache-Control':'no-store'})

    @app.get('/api/r2/uploader/install.ps1')
    def windows_installer(browser: str = ''):
        browser_argument(browser)
        if 'windows-x64' not in kits:
            raise HTTPException(503,'Windows 完整安装包尚未就绪，请联系管理员。')
        return Response(powershell(api,frontend,browser,kits),media_type='text/plain',headers={'Cache-Control':'no-store'})

    @app.get('/api/r2/uploader/bootstrap.py')
    def bootstrap():
        return Response((Path(__file__).parent/'upload_bootstrap.py').read_bytes(),media_type='text/plain')
