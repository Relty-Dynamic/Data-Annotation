[CmdletBinding()]
param(
    [string]$PythonPath,
    [switch]$SkipFrontend
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$originalLocation = Get-Location
$environmentNames = @('PYTHONNOUSERSITE', 'PIP_REQUIRE_VIRTUALENV', 'PIP_CACHE_DIR', 'NPM_CONFIG_CACHE', 'TEMP', 'TMP')
$originalEnvironment = @{}
foreach ($name in $environmentNames) { $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE." }
}
try {
    Set-Location -LiteralPath $projectRoot
    foreach ($relative in @('.cache\pip', '.cache\npm', '.cache\downloads', '.tmp', '.tools\ffmpeg', '.local\logs')) {
        New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot $relative) | Out-Null
    }
    $env:PYTHONNOUSERSITE = '1'
    $env:PIP_REQUIRE_VIRTUALENV = '1'
    $env:PIP_CACHE_DIR = Join-Path $projectRoot '.cache\pip'
    $env:NPM_CONFIG_CACHE = Join-Path $projectRoot '.cache\npm'
    $env:TEMP = Join-Path $projectRoot '.tmp'
    $env:TMP = $env:TEMP
    $manifest = Get-Content -LiteralPath (Join-Path $projectRoot 'runtime-manifest.json') -Raw | ConvertFrom-Json
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if (-not $PythonPath) {
            $pythonCommand = Get-Command python.exe -ErrorAction Stop
            $PythonPath = $pythonCommand.Source
        }
        & $PythonPath -c 'import sys; assert (3, 12) <= sys.version_info[:2] < (3, 15), "Python 3.12-3.14 is required"'
        Assert-NativeSuccess 'Python version check'
        & $PythonPath -m venv (Join-Path $projectRoot '.venv')
        Assert-NativeSuccess 'Virtual environment creation'
    }
    & $venvPython -c 'import sys,site; assert sys.prefix != sys.base_prefix, "Not a virtual environment"; assert not site.ENABLE_USER_SITE, "User site-packages must be disabled"'
    Assert-NativeSuccess 'Python isolation check'
    $dependencyFile = Join-Path $projectRoot 'requirements.lock.txt'
    if (-not (Test-Path -LiteralPath $dependencyFile)) { $dependencyFile = Join-Path $projectRoot 'requirements.txt' }
    & $venvPython -m pip install --requirement $dependencyFile
    Assert-NativeSuccess 'Python dependency installation'
    & $venvPython -m pip check
    Assert-NativeSuccess 'Python dependency verification'
    $ffmpegExe = Join-Path $projectRoot '.tools\ffmpeg\bin\ffmpeg.exe'
    $ffprobeExe = Join-Path $projectRoot '.tools\ffmpeg\bin\ffprobe.exe'
    if (-not (Test-Path -LiteralPath $ffmpegExe) -or -not (Test-Path -LiteralPath $ffprobeExe)) {
        $archivePath = Join-Path $projectRoot '.cache\downloads\ffmpeg-release-essentials.zip'
        $expectedHash = $manifest.ffmpeg.sha256
        $archiveValid = (Test-Path -LiteralPath $archivePath) -and ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -eq $expectedHash)
        if (-not $archiveValid) {
            Write-Host 'Downloading the pinned FFmpeg build into the project cache...'
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -UseBasicParsing -Uri $manifest.ffmpeg.url -OutFile $archivePath
        }
        if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $expectedHash) { throw 'FFmpeg checksum mismatch. The archive was not installed.' }
        $extractRoot = Join-Path $projectRoot ('.tmp\ffmpeg-install-' + $manifest.ffmpeg.version)
        New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
        Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
        $packageRoot = Get-ChildItem -LiteralPath $extractRoot -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin\ffmpeg.exe') } | Select-Object -First 1
        if (-not $packageRoot) { throw 'FFmpeg archive layout was not recognized.' }
        Get-ChildItem -LiteralPath $packageRoot.FullName | Copy-Item -Destination (Join-Path $projectRoot '.tools\ffmpeg') -Recurse -Force
    }
    $ffmpegLines = @(& $ffmpegExe -version)
    Assert-NativeSuccess 'FFmpeg verification'
    $ffprobeLines = @(& $ffprobeExe -version)
    Assert-NativeSuccess 'FFprobe verification'
    $ffmpegVersion = $ffmpegLines[0]
    $ffprobeVersion = $ffprobeLines[0]
    if (-not $ffmpegVersion.StartsWith('ffmpeg version ' + $manifest.ffmpeg.version) -or -not $ffprobeVersion.StartsWith('ffprobe version ' + $manifest.ffmpeg.version)) { throw 'Installed FFmpeg version does not match runtime-manifest.json.' }
    Write-Host $ffmpegVersion
    Write-Host $ffprobeVersion
    if (-not $SkipFrontend) {
        $nodePath = (Get-Command node.exe -ErrorAction Stop).Source
        $npmPath = (Get-Command npm.cmd -ErrorAction Stop).Source
        & $nodePath -e 'const [major,minor]=process.versions.node.split(".").map(Number); if(major<22 || (major===22 && minor<12)) throw new Error("Node.js 22.12 or later is required")'
        Assert-NativeSuccess 'Node.js version check'
        $frontendRoot = Join-Path $projectRoot 'frontend'
        if (Test-Path -LiteralPath (Join-Path $frontendRoot 'package-lock.json')) {
            & $npmPath --prefix $frontendRoot ci
        } else {
            & $npmPath --prefix $frontendRoot install
        }
        Assert-NativeSuccess 'Frontend dependency installation'
        & $npmPath --prefix $frontendRoot run build
        Assert-NativeSuccess 'Frontend build'
    }
    $shortcutShell = New-Object -ComObject WScript.Shell
    $shortcut = $shortcutShell.CreateShortcut((Join-Path $projectRoot '启动标注平台.lnk'))
    $shortcut.TargetPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
    $shortcut.Arguments = '"' + (Join-Path $projectRoot 'launch.py') + '"'
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'DataMark - closes automatically with the last browser tab'
    $shortcut.Save()
    Write-Host 'Setup complete. Open the DataMark shortcut in this folder.' -ForegroundColor Green
} finally {
    Set-Location -LiteralPath $originalLocation.Path
    foreach ($name in $environmentNames) { [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process') }
}


