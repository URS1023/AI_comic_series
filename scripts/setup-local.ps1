[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        python -m venv .venv
    }
    & '.venv\Scripts\python.exe' -m pip install --upgrade pip
    & '.venv\Scripts\python.exe' -m pip install -e '.[dev]'
    npm install
}
finally {
    Pop-Location
}

