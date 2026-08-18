[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$Revision = 'r001'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$visualMaster = Join-Path $projectRoot "renders\gaokao-rewind-ep01-visual-$Revision.mp4"
$finalVideo = Join-Path $projectRoot "renders\gaokao-rewind-ep01-$Revision.mp4"

if (Test-Path -LiteralPath $finalVideo) {
    throw "Revision already exists and will not be overwritten: $finalVideo"
}

Push-Location $projectRoot
try {
    $publishing = Get-Content -Raw -LiteralPath 'publishing\package.json' | ConvertFrom-Json
    foreach ($cover in $publishing.covers) {
        if ($cover.state -ne 'complete' -or -not (Test-Path -LiteralPath $cover.output)) {
            throw "Publishing cover is not complete: $($cover.id)"
        }
    }
    & $python 'scripts\verify_review_gates.py'
    if ($LASTEXITCODE -ne 0) { throw 'Hash-bound full visual-review gate failed.' }
    & $python 'scripts\qa_generated_media.py'
    if ($LASTEXITCODE -ne 0) { throw 'Generated-media quality gate failed.' }

    & $python 'scripts\build_ass_captions.py'
    node 'scripts\build-composition.mjs' . --strict-assets
    node 'scripts\stamp-seams.mjs' .
    node 'scripts\compact-generated-html.mjs' index.html
    npm run check -- --strict --snapshots
    if ($LASTEXITCODE -ne 0) { throw 'HyperFrames final check failed.' }

    node 'scripts\build-composition.mjs' . --strict-assets --no-html-captions
    node 'scripts\stamp-seams.mjs' .
    node 'scripts\compact-generated-html.mjs' index.html
    npm run render -- --quality high --output $visualMaster
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $visualMaster)) {
        throw 'HyperFrames visual-master render failed.'
    }

    ffmpeg -hide_banner -loglevel error -y -i $visualMaster -vf "ass=assets/captions/captions.ass:fontsdir=assets/fonts" -c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p -c:a copy -movflags +faststart $finalVideo
    if ($LASTEXITCODE -ne 0) { throw 'ASS caption burn failed.' }

    & $python 'scripts\qa_final_video.py' $finalVideo
    if ($LASTEXITCODE -ne 0) { throw 'Final encoded-video quality gate failed.' }
    $hbgVerifier = Join-Path $env:USERPROFILE '.codex\skills\hbg-life-simulation\scripts\verify_final_video.ps1'
    if (-not (Test-Path -LiteralPath $hbgVerifier)) {
        throw "HBG final verifier is missing: $hbgVerifier"
    }
    $storyboard = Get-Content -Raw -LiteralPath 'STORYBOARD_VIDEO.json' | ConvertFrom-Json
    $samples = @('0.5', '3.6', '6.2')
    $samples += @($storyboard | Where-Object highRisk | ForEach-Object { [string][Math]::Round(([double]$_.start + [double]$_.end) / 2, 3) })
    $samples += @([string][Math]::Round(([double]$storyboard[10].start + [double]$storyboard[10].end) / 2, 3), [string][Math]::Round(([double]$publishing.episode.durationSeconds - 0.8), 3))
    & $hbgVerifier -Video $finalVideo -QaDir (Join-Path $projectRoot "qa\hbg-final-$Revision") -Samples $samples
    if ($LASTEXITCODE -ne 0) { throw 'HBG Windows final verifier failed.' }
    & $python 'scripts\finalize_release.py' $finalVideo
    if ($LASTEXITCODE -ne 0) { throw 'Publishing package finalization failed.' }
}
finally {
    node 'scripts\build-composition.mjs' .
    node 'scripts\stamp-seams.mjs' .
    node 'scripts\compact-generated-html.mjs' index.html
    Pop-Location
}

Write-Output "FINAL_VIDEO=$finalVideo"
