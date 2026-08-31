# start-local.ps1 -- run the Functions host locally. Run from the repo root.
#
# Every line here is a lesson the sibling project already paid for:
#
#   VIRTUAL_ENV   Core Tools starts its OWN bundled Python unless the venv is
#                 ACTIVATED. Putting .venv\Scripts on PATH is not enough -- the
#                 host looks for VIRTUAL_ENV. Without it you get a
#                 ModuleNotFoundError for a package you definitely installed.
#   PYTHONPATH    loads scripts\localshim\sitecustomize.py into the worker, which
#                 the host spawns, so your shell's environment does not reach it.
#
# Ctrl+C stops the host.

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Error "No virtual environment found. Create one first:`n  py -m venv .venv`n  .\.venv\Scripts\python.exe -m pip install -r functionapp\requirements.txt"
}

if (-not (Test-Path ".\functionapp\local.settings.json")) {
    Write-Warning "functionapp\local.settings.json is missing. Copy the template and fill it in:"
    Write-Warning "  Copy-Item functionapp\local.settings.json.template functionapp\local.settings.json"
}

$env:VIRTUAL_ENV = "$PWD\.venv"
$env:PATH        = "$PWD\.venv\Scripts;$env:PATH"
$env:PYTHONPATH  = "$PWD\scripts\localshim"

Write-Host "interpreter : " -NoNewline
& ".\.venv\Scripts\python.exe" -c "import sys; print(sys.executable)"
Write-Host "shim        : $env:PYTHONPATH"
Write-Host ""

Set-Location functionapp
func start
