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
        throw '剪贴板/标准输入中没有 Copy-as-cURL。请先在浏览器开发者工具中复制完整 cURL。'
    }

    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $python
    $start.WorkingDirectory = $projectRoot
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.Environment['PYTHONPATH'] = Join-Path $projectRoot 'src'
    [void]$start.ArgumentList.Add('-m')
    [void]$start.ArgumentList.Add('ai_comic_series.session')
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

    Write-Host '长期会话已启动；等待 preflight=200 的 ready 消息后，输入单行 JSON 命令。输入 {"action":"quit"} 退出。'
    while (-not $process.HasExited) {
        $command = Read-Host 'session JSON'
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
