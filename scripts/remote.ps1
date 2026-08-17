[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ComicArgs
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
$createdSecret = $false
$secretPointer = [IntPtr]::Zero

try {
    if (-not $env:AI_COMIC_JUPYTER_COOKIE) {
        $secureCookie = Read-Host '粘贴最新 AMD 请求中的完整 Cookie 值（隐藏输入，不会保存）' -AsSecureString
        $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureCookie)
        $env:AI_COMIC_JUPYTER_COOKIE = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
        $createdSecret = $true
    }
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    Push-Location $projectRoot
    try {
        & $python -m ai_comic_series @ComicArgs
        exit $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($createdSecret) {
        Remove-Item Env:AI_COMIC_JUPYTER_COOKIE -ErrorAction SilentlyContinue
    }
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    if ($secretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
}

