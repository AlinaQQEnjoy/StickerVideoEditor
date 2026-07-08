param(
    [Parameter(Mandatory = $true)]
    [string]$InputVideo,
    [string]$OutputDir = "sticker_action_output",
    [string]$FinalVideo = "sticker_action_output\sticker_action_cut.mp4",
    [string]$FfmpegExe = "D:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    [string]$PythonExe = "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [string]$Roi = "0,0,1,1",
    [double]$AnalysisFps = 12.0,
    [int]$MaxActions = 80,
    [double]$MinActionGap = 0.35,
    [double]$PeelDuration = 1.0,
    [double]$RepeatPeelDuration = 2.0,
    [double]$PeelBefore = 0.35,
    [double]$ReleaseDuration = 0.8,
    [double]$SameStickerGap = 1.75,
    [double]$SameStickerRetryGap = 2.0,
    [double]$SameStickerDistance = 0.22,
    [double]$FinalClusterMaxSpan = 2.2,
    [double]$RepeatPeelWindow = 1.05,
    [double]$MotionThreshold = 1.4,
    [double]$AudioThreshold = 2.4,
    [switch]$DisableAudio
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $PSCommandPath
$InvariantCulture = [System.Globalization.CultureInfo]::InvariantCulture

function Format-FfmpegSeconds {
    param([double]$Seconds)
    return $Seconds.ToString("0.000", $InvariantCulture)
}

function Resolve-CommandPath {
    param([string]$Command, [string]$Name)
    if (Test-Path -LiteralPath $Command) {
        return (Resolve-Path -LiteralPath $Command).Path
    }
    $found = Get-Command $Command -ErrorAction SilentlyContinue
    if ($null -ne $found) {
        return $found.Source
    }
    throw "$Name not found: $Command"
}

function Convert-TimeTextToSeconds {
    param([string]$Text)
    $parts = $Text.Trim() -split "[:.]"
    if ($parts.Count -lt 4) {
        return [double]::Parse($Text, [System.Globalization.CultureInfo]::InvariantCulture)
    }
    return ([int]$parts[0] * 3600) + ([int]$parts[1] * 60) + [int]$parts[2] + ([int]$parts[3] / 1000.0)
}

if (-not (Test-Path -LiteralPath $InputVideo)) {
    throw "Input video not found: $InputVideo"
}

if (-not [System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir = Join-Path $ScriptDir $OutputDir
}
if (-not [System.IO.Path]::IsPathRooted($FinalVideo)) {
    $FinalVideo = Join-Path $ScriptDir $FinalVideo
}

$ffmpeg = Resolve-CommandPath -Command $FfmpegExe -Name "FFmpeg"
$python = Resolve-CommandPath -Command $PythonExe -Name "Python"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$detectorArgs = @(
    (Join-Path $ScriptDir "sticker_action_detector.py"),
    "--input", $InputVideo,
    "--output-dir", $OutputDir,
    "--ffmpeg", $ffmpeg,
    "--roi", $Roi,
    "--analysis-fps", $AnalysisFps,
    "--max-actions", $MaxActions,
    "--min-action-gap", $MinActionGap,
    "--peel-duration", $PeelDuration,
    "--repeat-peel-duration", $RepeatPeelDuration,
    "--peel-before", $PeelBefore,
    "--release-duration", $ReleaseDuration,
    "--same-sticker-gap", $SameStickerGap,
    "--same-sticker-retry-gap", $SameStickerRetryGap,
    "--same-sticker-distance", $SameStickerDistance,
    "--final-cluster-max-span", $FinalClusterMaxSpan,
    "--repeat-peel-window", $RepeatPeelWindow,
    "--motion-threshold", $MotionThreshold,
    "--audio-threshold", $AudioThreshold
)

if ($DisableAudio) {
    $detectorArgs += "--disable-audio"
}

Write-Host "Detecting sticker peel/release edit points..."
& $python @detectorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Detection failed with exit code $LASTEXITCODE."
}

$segmentsPath = Join-Path $OutputDir "segments.csv"
$segments = Import-Csv -LiteralPath $segmentsPath
if ($segments.Count -eq 0) {
    throw "No edit segments were detected. Try lowering -MotionThreshold or -AudioThreshold."
}

$clipsDir = Join-Path $OutputDir "clips"
New-Item -ItemType Directory -Force -Path $clipsDir | Out-Null
Get-ChildItem -LiteralPath $clipsDir -Filter "clip_*.mp4" -ErrorAction SilentlyContinue | Remove-Item -Force

$concatPath = Join-Path $OutputDir "concat_list.txt"
$concatLines = New-Object System.Collections.Generic.List[string]

Write-Host "Cutting $($segments.Count) clips..."
for ($i = 0; $i -lt $segments.Count; $i++) {
    $start = Convert-TimeTextToSeconds $segments[$i].start
    $duration = [double]::Parse($segments[$i].duration, [System.Globalization.CultureInfo]::InvariantCulture)
    $clipName = "clip_{0:D3}_{1}.mp4" -f ($i + 1), $segments[$i].kind.Replace("+", "_")
    $clipPath = Join-Path $clipsDir $clipName

    & $ffmpeg -hide_banner -loglevel error -y `
        -ss (Format-FfmpegSeconds $start) `
        -i $InputVideo `
        -t (Format-FfmpegSeconds $duration) `
        -c:v libx264 -preset veryfast -crf 20 `
        -c:a aac -b:a 160k `
        -movflags +faststart `
        $clipPath

    if ($LASTEXITCODE -ne 0) {
        throw "Cutting clip $($i + 1) failed with exit code $LASTEXITCODE."
    }

    $safeClipPath = ([System.IO.Path]::GetFullPath($clipPath)).Replace("\", "/").Replace("'", "'\''")
    $concatLines.Add("file '$safeClipPath'")
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines([System.IO.Path]::GetFullPath($concatPath), [string[]]$concatLines, $utf8NoBom)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $FinalVideo) | Out-Null

Write-Host "Joining clips..."
& $ffmpeg -hide_banner -loglevel error -y `
    -f concat -safe 0 `
    -i $concatPath `
    -c:v libx264 -preset veryfast -crf 20 `
    -c:a aac -b:a 160k `
    -movflags +faststart `
    $FinalVideo

if ($LASTEXITCODE -ne 0) {
    throw "Joining clips failed with exit code $LASTEXITCODE."
}

Write-Host "Done."
Write-Host "Final video: $FinalVideo"
Write-Host "Segments: $segmentsPath"

try {
    $refreshPayload = @{
        inputVideo = [System.IO.Path]::GetFullPath($InputVideo)
        outputDir = [System.IO.Path]::GetFullPath($OutputDir)
        finalVideo = [System.IO.Path]::GetFullPath($FinalVideo)
    } | ConvertTo-Json -Compress

    Invoke-RestMethod `
        -Uri "http://127.0.0.1:8787/api/load-output" `
        -Method Post `
        -ContentType "application/json; charset=utf-8" `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($refreshPayload)) `
        -TimeoutSec 5 | Out-Null

    Write-Host "Review UI refreshed: http://127.0.0.1:8787"
}
catch {
    Write-Warning "Could not refresh review UI at http://127.0.0.1:8787. Start sticker_review_server.py and refresh manually."
}
