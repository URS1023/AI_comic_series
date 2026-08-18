[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { (Get-Command python).Source }
$curlCommand = $null
$process = $null

try {
    $pipedLines = @($input)
    if ($pipedLines.Count -gt 0) {
        $curlCommand = $pipedLines -join [Environment]::NewLine
    }
    else {
        $curlCommand = Get-Clipboard -Raw
    }
    if ([string]::IsNullOrWhiteSpace($curlCommand)) {
        throw '剪贴板/标准输入中没有 api/contents Copy-as-cURL。'
    }

    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $python
    $start.WorkingDirectory = $projectRoot
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.Environment['PYTHONPATH'] = Join-Path $projectRoot 'src'
    [void]$start.ArgumentList.Add('-m')
    [void]$start.ArgumentList.Add('ai_comic_series.direct_session')
    [void]$start.ArgumentList.Add('--curl-stdin')
    [void]$start.ArgumentList.Add('config/project.toml')

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    [void]$process.Start()
    $process.StandardInput.Write($curlCommand)
    if (-not $curlCommand.EndsWith("`n")) {
        $process.StandardInput.WriteLine()
    }
    $process.StandardInput.WriteLine('__AI_COMIC_CURL_END__')
    $process.StandardInput.Flush()
    $curlCommand = $null

    Write-Host '正在一次性引导直连代理；出现 transport=direct-https 后 Jupyter 已关闭。'
    while (-not $process.HasExited) {
        $command = Read-Host 'direct JSON'
        if ($process.HasExited) { break }
        if ([string]::IsNullOrWhiteSpace($command)) { continue }
        $process.StandardInput.WriteLine($command)
        $process.StandardInput.Flush()
        if ($command -match '"action"\s*:\s*"quit"') { break }
    }
    $process.StandardInput.Close()
    $process.WaitForExit()
    exit $process.ExitCode
}
finally {
    $curlCommand = $null
    if ($null -ne $process) {
        $process.Dispose()
    }
}
