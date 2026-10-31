$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $env:PYTHONUTF8 = '1'
    & uv sync --frozen --no-dev --no-install-project --python 3.12 --link-mode copy
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & '.\.venv\Scripts\python.exe' -B '.\sim_bim.py' doctor
    if ($LASTEXITCODE -ne 0) { throw 'Installation verification failed' }
} finally { Pop-Location }
