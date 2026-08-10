param(
    [Parameter(Mandatory = $true)]
    [string]$InputVideo,
    [string]$OutputDir = "sticker_action_output",
    [string]$FinalVideo = "",
    [string]$FfmpegExe = "D:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    [string]$PythonExe = "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [double]$ThresholdDbfs = -20.0,
    [double]$PreRoll = 0.4,
    [double]$PostRoll = 0.4,
    [double]$MinGap = 0.3,
    [double]$WindowMs = 10.0,
    [int]$SampleRate = 48000,
    [int]$MaxPeaks = 300,
    [double]$BoostBelowDbfs = -4.0,
    [double]$BoostGainDb = 20.0,
    [string]$VideoEncoder = "h264_nvenc",
    [double]$VideoQuality = 16.0,
    [int]$X264Threads = 1,
    [bool]$UpscaleTo4K = $true
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $PSCommandPath
$InvariantCulture = [System.Globalization.CultureInfo]::InvariantCulture

function Format-FfmpegSeconds {
    param([double]$Seconds)
    return $Seconds.ToString("0.000", $InvariantCulture)
}

function Format-InvariantNumber {
    param([double]$Value)
    return $Value.ToString("0.###", $InvariantCulture)
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
        return [double]::Parse($Text, $InvariantCulture)
    }
    return ([int]$parts[0] * 3600) + ([int]$parts[1] * 60) + [int]$parts[2] + ([int]$parts[3] / 1000.0)
}

function Convert-ScoreToDbfs {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) {
        return 0.0
    }
    $match = [regex]::Match($Text, "[-+]?\d+(?:\.\d+)?")
    if (-not $match.Success) {
        return 0.0
    }
    return [double]::Parse($match.Value, $InvariantCulture)
}

function Get-VideoEncodeArgs {
    if ($VideoEncoder -eq "h264_nvenc") {
        return @(
            "-c:v", "h264_nvenc",
            "-preset", "p5",
            "-cq:v", (Format-InvariantNumber $VideoQuality),
            "-b:v", "0",
            "-pix_fmt", "yuv420p"
        )
    }
    return @(
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", (Format-InvariantNumber $VideoQuality),
        "-threads", $X264Threads,
        "-pix_fmt", "yuv420p"
    )
}

function Resolve-FfprobePath {
    param([string]$FfmpegPath)
    $candidate = Join-Path (Split-Path -Parent $FfmpegPath) "ffprobe.exe"
    if (Test-Path -LiteralPath $candidate) {
        return (Resolve-Path -LiteralPath $candidate).Path
    }
    return (Resolve-CommandPath -Command "ffprobe" -Name "FFprobe")
}

function Get-VideoScaleArgs {
    param([string]$VideoPath, [string]$FfmpegPath)
    if (-not $UpscaleTo4K) {
        return @()
    }

    $ffprobe = Resolve-FfprobePath -FfmpegPath $FfmpegPath
    $probeText = (& $ffprobe -v error -select_streams v:0 -show_entries "stream=width,height:stream_tags=rotate:stream_side_data=rotation" -of json $VideoPath)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($probeText)) {
        throw "Could not read source video dimensions."
    }
    $probe = $probeText | ConvertFrom-Json
    $stream = @($probe.streams)[0]
    $width = [int]$stream.width
    $height = [int]$stream.height
    $rotation = 0
    foreach ($sideData in @($stream.side_data_list)) {
        if ($null -ne $sideData.rotation) {
            $rotation = [int][double]$sideData.rotation
            break
        }
    }
    if ($rotation -eq 0 -and $null -ne $stream.tags -and $null -ne $stream.tags.rotate) {
        $rotation = [int][double]$stream.tags.rotate
    }
    $displayWidth = $width
    $displayHeight = $height
    if ([Math]::Abs($rotation) -eq 90 -or [Math]::Abs($rotation) -eq 270) {
        $displayWidth = $height
        $displayHeight = $width
    }
    $targetWidth = 3840
    $targetHeight = 2160
    if ($displayHeight -ge $displayWidth) {
        $targetWidth = 2160
        $targetHeight = 3840
    }
    if ($rotation -eq 0 -and $displayWidth -ge $targetWidth -and $displayHeight -ge $targetHeight) {
        Write-Host "Source is already 4K or higher: ${displayWidth}x${displayHeight}"
        return @()
    }
    $scale = "scale=${targetWidth}:${targetHeight}:flags=lanczos"
    Write-Host "Normalizing clips to 4K: ${displayWidth}x${displayHeight}, rotation ${rotation} -> ${targetWidth}x${targetHeight}"
    return @("-vf", $scale)
}

if (-not (Test-Path -LiteralPath $InputVideo)) {
    throw "Input video not found: $InputVideo"
}

if (-not [System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir = Join-Path $ScriptDir $OutputDir
}
if ([string]::IsNullOrWhiteSpace($FinalVideo)) {
    $dateFolder = Get-Date -Format "yyyy-MM-dd"
    $editedFolderName = -join ([char[]](0x526A, 0x8F91, 0x540E, 0x89C6, 0x9891))
    $sourceName = [System.IO.Path]::GetFileNameWithoutExtension($InputVideo)
    $stamp = Get-Date -Format "HHmmss"
    $FinalVideo = Join-Path (Join-Path (Join-Path "D:\MellowScape" $editedFolderName) $dateFolder) ("{0}_audio_peak_cut_{1}.mp4" -f $sourceName, $stamp)
}
if (-not [System.IO.Path]::IsPathRooted($FinalVideo)) {
    $FinalVideo = Join-Path $ScriptDir $FinalVideo
}

$ffmpeg = Resolve-CommandPath -Command $FfmpegExe -Name "FFmpeg"
$python = Resolve-CommandPath -Command $PythonExe -Name "Python"
$videoScaleArgs = Get-VideoScaleArgs -VideoPath $InputVideo -FfmpegPath $ffmpeg
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$detectorArgs = @(
    (Join-Path $ScriptDir "audio_peak_detector.py"),
    "--input", $InputVideo,
    "--output-dir", $OutputDir,
    "--ffmpeg", $ffmpeg,
    "--threshold-dbfs", (Format-InvariantNumber $ThresholdDbfs),
    "--pre-roll", (Format-InvariantNumber $PreRoll),
    "--post-roll", (Format-InvariantNumber $PostRoll),
    "--min-gap", (Format-InvariantNumber $MinGap),
    "--window-ms", (Format-InvariantNumber $WindowMs),
    "--sample-rate", $SampleRate,
    "--max-peaks", $MaxPeaks
)

Write-Host "Detecting audio peaks..."
& $python @detectorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Audio peak detection failed with exit code $LASTEXITCODE."
}

$segmentsPath = Join-Path $OutputDir "segments.csv"
$segments = @(Import-Csv -LiteralPath $segmentsPath)
if ($segments.Count -eq 0) {
    throw "No audio peaks were detected. Try lowering -ThresholdDbfs, for example -24."
}

$clipsDir = Join-Path $OutputDir "clips"
$thumbsDir = Join-Path $OutputDir "thumbs"
New-Item -ItemType Directory -Force -Path $clipsDir | Out-Null
New-Item -ItemType Directory -Force -Path $thumbsDir | Out-Null
Get-ChildItem -LiteralPath $clipsDir -Filter "clip_*.mp4" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem -LiteralPath $clipsDir -Filter "segment_*.mp4" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem -LiteralPath $thumbsDir -Filter "segment_*.jpg" -ErrorAction SilentlyContinue | Remove-Item -Force

$concatPath = Join-Path $OutputDir "concat_list.txt"
$concatLines = New-Object System.Collections.Generic.List[string]
$videoArgs = Get-VideoEncodeArgs

Write-Host "Cutting $($segments.Count) audio peak clips..."
for ($i = 0; $i -lt $segments.Count; $i++) {
    $start = Convert-TimeTextToSeconds $segments[$i].start
    $duration = [double]::Parse($segments[$i].duration, $InvariantCulture)
    $peakDbfs = Convert-ScoreToDbfs $segments[$i].score
    $audioArgs = @("-c:a", "aac", "-b:a", "160k")
    if ($peakDbfs -lt $BoostBelowDbfs) {
        $gainFilter = "volume={0}dB" -f (Format-InvariantNumber $BoostGainDb)
        $audioArgs = @("-af", $gainFilter, "-c:a", "aac", "-b:a", "160k")
        Write-Host ("Boosting clip {0} audio by +{1}dB, peak {2} dBFS" -f ($i + 1), (Format-InvariantNumber $BoostGainDb), $peakDbfs.ToString("0.0", $InvariantCulture))
    }
    $clipName = "clip_{0:D3}_{1}.mp4" -f ($i + 1), $segments[$i].kind.Replace("+", "_")
    $clipPath = Join-Path $clipsDir $clipName

    $cutArgs = @(
        "-hide_banner", "-loglevel", "error", "-y",
        "-ss", (Format-FfmpegSeconds $start),
        "-i", $InputVideo,
        "-t", (Format-FfmpegSeconds $duration)
    ) + $videoScaleArgs + $videoArgs + $audioArgs + @(
        "-movflags", "+faststart",
        $clipPath
    )

    & $ffmpeg @cutArgs

    if ($LASTEXITCODE -ne 0) {
        throw "Cutting clip $($i + 1) failed with exit code $LASTEXITCODE."
    }

    $thumbTime = $start + [Math]::Min($duration * 0.45, 0.35)
    $thumbPath = Join-Path $thumbsDir ("segment_{0:D3}.jpg" -f ($i + 1))
    & $ffmpeg -hide_banner -loglevel error -y `
        -ss (Format-FfmpegSeconds $thumbTime) `
        -i $InputVideo `
        -frames:v 1 `
        -vf "scale=360:-2" `
        -q:v 3 `
        $thumbPath

    if ($LASTEXITCODE -ne 0) {
        throw "Generating thumbnail $($i + 1) failed with exit code $LASTEXITCODE."
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
    -c copy `
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
