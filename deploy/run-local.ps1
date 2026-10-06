$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
Set-Location -LiteralPath $projectPath
# Middleware emits safe request logs without raw URLs or query strings.
& $pythonPath -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --log-level debug --no-access-log
exit $LASTEXITCODE
