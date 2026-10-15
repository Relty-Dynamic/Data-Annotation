"""Public, credential-free installers; every runtime stays in one project folder."""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

from fastapi.responses import Response

MAC_TOOLS = {
 'arm64': ('1789931890', {'ffmpeg':'c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924','ffprobe':'fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6'}),
 'amd64': ('1789931006', {'ffmpeg':'7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4','ffprobe':'2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd'})}


def bundle(root):
    buffer=io.BytesIO()
    root=Path(root)
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((root/'backend').rglob('*')):
            if (path.is_file() and not path.is_symlink() and not path.name.startswith('test_')
                    and not path.name.endswith('.test.mjs') and '__pycache__' not in path.parts
                    and not any(part.startswith('.') for part in path.relative_to(root).parts)
                    and path.suffix in {'.py','.html','.css','.js','.mjs'}):
                archive.writestr(path.relative_to(root).as_posix(),path.read_bytes())
        archive.writestr('requirements.lock.txt',(root/'requirements.lock.txt').read_bytes())
    return buffer.getvalue()


def shell(api,frontend):
    return f'''#!/bin/sh
set -eu
root="$HOME/Desktop/DataMarkUploader"
mkdir -p "$root/.tools/uv" "$root/.tools/python" "$root/.cache/uv" "$root/.tmp"
export UV_INSTALL_DIR="$root/.tools/uv" UV_NO_MODIFY_PATH=1 UV_NO_CONFIG=1
export UV_PYTHON_INSTALL_DIR="$root/.tools/python" UV_PYTHON_BIN_DIR="$root/.tools/python-bin"
export UV_CACHE_DIR="$root/.cache/uv" PYTHONNOUSERSITE=1 PIP_REQUIRE_VIRTUALENV=1
export PIP_CACHE_DIR="$root/.cache/pip" TMPDIR="$root/.tmp"
if [ ! -x "$root/.tools/uv/uv" ]; then
 curl --fail --location --show-error https://astral.sh/uv/0.12.5/install.sh -o "$root/.tmp/uv-install.sh"
 sh "$root/.tmp/uv-install.sh"
fi
if [ ! -x "$root/.venv/bin/python" ]; then
 "$root/.tools/uv/uv" --no-config venv --python 3.14.6 "$root/.venv"
fi
"$root/.tools/uv/uv" --no-config pip install --python "$root/.venv/bin/python" --index-url https://pypi.tuna.tsinghua.edu.cn/simple 'pip==26.2.1'
curl --fail --location --show-error '{api}/api/r2/uploader/bootstrap.py' -o "$root/.tmp/bootstrap.py"
exec "$root/.venv/bin/python" "$root/.tmp/bootstrap.py" --root "$root" --api '{api}' --frontend '{frontend}'
'''


def powershell(api,frontend):
    return f'''$ErrorActionPreference = 'Stop'
$root = Join-Path ([Environment]::GetFolderPath('Desktop')) 'DataMarkUploader'
$names = @('UV_INSTALL_DIR','UV_NO_MODIFY_PATH','UV_NO_CONFIG','UV_PYTHON_INSTALL_DIR','UV_PYTHON_BIN_DIR','UV_CACHE_DIR','PYTHONNOUSERSITE','PIP_REQUIRE_VIRTUALENV','PIP_CACHE_DIR','TEMP','TMP')
$saved = @{{}}
foreach ($name in $names) {{ $saved[$name] = [Environment]::GetEnvironmentVariable($name,'Process') }}
try {{
 foreach ($folder in @('.tools/uv','.tools/python','.cache/uv','.tmp')) {{ New-Item -ItemType Directory -Force (Join-Path $root $folder) | Out-Null }}
 $env:UV_INSTALL_DIR = Join-Path $root '.tools/uv'; $env:UV_NO_MODIFY_PATH='1'; $env:UV_NO_CONFIG='1'
 $env:UV_PYTHON_INSTALL_DIR=Join-Path $root '.tools/python'; $env:UV_PYTHON_BIN_DIR=Join-Path $root '.tools/python-bin'
 $env:UV_CACHE_DIR=Join-Path $root '.cache/uv'; $env:PYTHONNOUSERSITE='1'; $env:PIP_REQUIRE_VIRTUALENV='1'
 $env:PIP_CACHE_DIR=Join-Path $root '.cache/pip'; $env:TEMP=Join-Path $root '.tmp'; $env:TMP=$env:TEMP
 $uv=Join-Path $root '.tools/uv/uv.exe'
 if (-not (Test-Path $uv)) {{ & ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing 'https://astral.sh/uv/0.12.5/install.ps1').Content)) }}
 $python=Join-Path $root '.venv/Scripts/python.exe'
 if (-not (Test-Path $python)) {{ & $uv --no-config venv --python 3.14.6 (Join-Path $root '.venv'); if ($LASTEXITCODE -ne 0) {{ throw 'Python installation failed' }} }}
 & $uv --no-config pip install --python $python --index-url https://pypi.tuna.tsinghua.edu.cn/simple 'pip==26.2.1'
 if ($LASTEXITCODE -ne 0) {{ throw 'pip installation failed' }}
 $bootstrap=Join-Path $root '.tmp/bootstrap.py'
 Invoke-WebRequest -UseBasicParsing '{api}/api/r2/uploader/bootstrap.py' -OutFile $bootstrap
 & $python $bootstrap --root $root --api '{api}' --frontend '{frontend}'
 if ($LASTEXITCODE -ne 0) {{ throw 'DataMark uploader installation or startup failed' }}
}} finally {{ foreach ($name in $names) {{ [Environment]::SetEnvironmentVariable($name,$saved[$name],'Process') }} }}
'''


def install(app,root):
    from .public_setup import public_origin
    api=public_origin(os.environ['DATAMARK_PUBLIC_API_ORIGIN'],api=True)
    frontend=public_origin(os.environ['DATAMARK_PUBLIC_ORIGIN'])
    package=bundle(root)

    @app.get('/api/r2/uploader/bundle-info')
    def info():
        return {'sha256':hashlib.sha256(package).hexdigest(),'size':len(package)}

    @app.get('/api/r2/uploader/bundle.zip')
    def archive():
        return Response(package,media_type='application/zip')

    @app.get('/api/r2/uploader/install.sh')
    def mac_installer():
        return Response(shell(api,frontend),media_type='text/plain')

    @app.get('/api/r2/uploader/install.ps1')
    def windows_installer():
        return Response(powershell(api,frontend),media_type='text/plain')

    @app.get('/api/r2/uploader/bootstrap.py')
    def bootstrap():
        return Response((Path(__file__).parent/'upload_bootstrap.py').read_bytes(),media_type='text/plain')
