$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Recording = Join-Path $PSScriptRoot ".recording"
$Proof = Join-Path $PSScriptRoot "video-proof"
$MobileUrl = if ($env:FIELDLEDGER_MOBILE_URL) { $env:FIELDLEDGER_MOBILE_URL } else { "http://127.0.0.1:4173" }
$WordUrl = if ($env:FIELDLEDGER_WORD_URL) { $env:FIELDLEDGER_WORD_URL } else { "http://127.0.0.1:4174" }

function Wait-Url([string] $Url) {
  for ($Attempt = 0; $Attempt -lt 120; $Attempt++) {
    try {
      $Response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
      if ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 400) { return }
    } catch { Start-Sleep -Seconds 1 }
  }
  throw "Readiness timed out for $Url. Start a freshly reset deterministic Compose stack first."
}

$Ffmpeg = Get-ChildItem "$env:LOCALAPPDATA/Microsoft/WinGet/Packages" -Recurse -Filter ffmpeg.exe |
  Where-Object FullName -Match "Gyan.FFmpeg" |
  Select-Object -Last 1 -ExpandProperty FullName
if (-not $Ffmpeg) { throw "FFmpeg Essentials is required to create and inspect the H.264 walkthrough." }

New-Item -ItemType Directory -Force -Path $Recording, $Proof | Out-Null
Wait-Url "$MobileUrl/inbox"
Wait-Url "$WordUrl/taskpane.html"

$env:FIELDLEDGER_MOBILE_URL = $MobileUrl
$env:FIELDLEDGER_WORD_URL = $WordUrl
$RawVideo = (& node "$PSScriptRoot/record-demo.mjs" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $RawVideo)) {
  throw "Browser recording failed. Reset synthetic state, restart the deterministic stack, and retry."
}

Add-Type -AssemblyName System.Speech
$Speech = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$Speech.SelectVoice("Microsoft David Desktop")
$Speech.Rate = -1
$Wave = Join-Path $Recording "narration.wav"
$Speech.SetOutputToWaveFile($Wave)
$Speech.Speak((Get-Content -Raw "$PSScriptRoot/narration.txt"))
$Speech.Dispose()

$Output = Join-Path $PSScriptRoot "fieldledger-superdocs-demo.mp4"
& $Ffmpeg -y -i $RawVideo -i $Wave -vf "tpad=stop_mode=clone:stop_duration=30,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2" -af "apad=pad_dur=30" -t 230 -r 30 -c:v libx264 -preset medium -crf 21 -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart $Output
if ($LASTEXITCODE -ne 0) { throw "Video encoding failed." }

$FramePaths = @()
$ProofFrames = [ordered]@{
  10 = "frame-010.png"
  30 = "frame-030.png"
  55 = "frame-055.png"
  90 = "frame-090.png"
  125 = "frame-125.png"
  150 = "frame-150.png"
  180 = "frame-180.png"
  210 = "frame-210.png"
}
foreach ($Entry in $ProofFrames.GetEnumerator()) {
  $Second = $Entry.Key
  $Frame = Join-Path $Proof $Entry.Value
  & $Ffmpeg -y -ss $Second -i $Output -frames:v 1 -update 1 $Frame
  if ($LASTEXITCODE -ne 0) { throw "Proof frame extraction failed at $Second seconds." }
  $FramePaths += $Frame
}

$Contact = Join-Path $Proof "contact-sheet.png"
$ContactArgs = @("-y")
foreach ($Frame in $FramePaths) { $ContactArgs += @("-i", $Frame) }
$ContactArgs += @(
  "-filter_complex",
  "[0:v]scale=480:270[a0];[1:v]scale=480:270[a1];[2:v]scale=480:270[a2];[3:v]scale=480:270[a3];[4:v]scale=480:270[a4];[5:v]scale=480:270[a5];[6:v]scale=480:270[a6];[7:v]scale=480:270[a7];[a0][a1][a2][a3]hstack=inputs=4[top];[a4][a5][a6][a7]hstack=inputs=4[bottom];[top][bottom]vstack=inputs=2",
  "-frames:v", "1", "-update", "1", $Contact
)
& $Ffmpeg @ContactArgs
if ($LASTEXITCODE -ne 0) { throw "Video proof contact sheet creation failed." }

Write-Output $Output
