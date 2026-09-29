param(
    [switch]$PrepareOnly,
    [switch]$SkipReadyPrompt
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Project Python is missing. Run the project setup first.'
}

if (-not $PrepareOnly -and -not $SkipReadyPrompt) {
    Add-Type -AssemblyName PresentationFramework
    $answer = [System.Windows.MessageBox]::Show(
        'Ready to log in to relty@192.168.2.25? Click OK, then enter the server password in this console. The password will not be recorded. Keep this window open until deployment completes.',
        'Datamark server deployment',
        [System.Windows.MessageBoxButton]::OKCancel,
        [System.Windows.MessageBoxImage]::Information
    )
    if ($answer -ne [System.Windows.MessageBoxResult]::OK) { exit 2 }
}

$processEnvironment = @{
    PYTHONNOUSERSITE = '1'
    PYTHONDONTWRITEBYTECODE = '1'
    PIP_REQUIRE_VIRTUALENV = '1'
    PIP_CACHE_DIR = (Join-Path $projectRoot '.cache\pip')
    NPM_CONFIG_CACHE = (Join-Path $projectRoot '.cache\npm')
    UV_CACHE_DIR = (Join-Path $projectRoot '.cache\uv')
    UV_PYTHON_INSTALL_DIR = (Join-Path $projectRoot '.tools\uv-python')
    TEMP = (Join-Path $projectRoot '.tmp')
    TMP = (Join-Path $projectRoot '.tmp')
}
[System.IO.Directory]::CreateDirectory($processEnvironment.TEMP) > $null
$previousEnvironment = @{}
$arguments = @('-s', (Join-Path $PSScriptRoot 'deploy_worker.py'))
if ($PrepareOnly) { $arguments += '--prepare-only' }
try {
    foreach ($name in $processEnvironment.Keys) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $processEnvironment[$name], 'Process')
    }
    & $pythonPath @arguments
    $deploymentExitCode = $LASTEXITCODE
} finally {
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}
exit $deploymentExitCode
