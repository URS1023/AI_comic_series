[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ComicArgs
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
$curlCommand = $null

try {
    $pipedLines = @($input)
    if ($pipedLines.Count -gt 0) {
        $curlCommand = $pipedLines -join [Environment]::NewLine
    }
    else {
        $curlCommand = Get-Clipboard -Raw
    }
    if ([string]::IsNullOrWhiteSpace($curlCommand)) {
        throw '剪贴板/标准输入中没有 Copy-as-cURL。请先在浏览器开发者工具中复制完整 cURL。'
    }

    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    Push-Location $projectRoot
    try {
        # The complete cURL enters only through the child process stdin. It is
        # never placed in argv, an environment variable, a file, or output.
        $curlCommand | & $python -m ai_comic_series --curl-stdin @ComicArgs
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    exit $exitCode
}
finally {
    $curlCommand = $null
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
}
