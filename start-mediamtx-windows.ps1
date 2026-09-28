# Sobe o MediaMTX no host Windows, que enxerga a camera na mesma sub-rede e
# consegue receber o RTP/UDP. So e necessario quando o monitor roda em WSL2.
# Uso:  powershell -ExecutionPolicy Bypass -File .\start-mediamtx-windows.ps1
$ErrorActionPreference = 'Stop'

# As credenciais ficam so no .env do repositorio; nada e gravado no disco do
# Windows alem do proprio binario.
$cfg = @{}
Get-Content (Join-Path $PSScriptRoot '.env') | ForEach-Object {
    if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$') { $cfg[$Matches[1]] = $Matches[2] }
}
$user = [uri]::EscapeDataString($cfg['CAM_USER'])
$pass = [uri]::EscapeDataString($cfg['CAM_PASS'])
$port = if ($cfg['CAM_RTSP_PORT']) { $cfg['CAM_RTSP_PORT'] } else { '554' }
$path = if ($cfg['CAM_PATH']) { $cfg['CAM_PATH'] } else { 'onvif1' }
$env:MTX_PATHS_CAMERA_SOURCE = "rtsp://${user}:${pass}@$($cfg['CAM_HOST']):${port}/${path}"
# A camera so entrega RTP por UDP: recusa TCP interleaved no SETUP.
$env:MTX_PATHS_CAMERA_RTSPTRANSPORT = 'udp'

# O MediaMTX observa o diretorio de config com ReadDirectoryChanges, que o 9p do
# \\wsl.localhost nao implementa. Por isso ele roda a partir do disco do Windows.
$stage = Join-Path $env:LOCALAPPDATA 'camera-monitor-relay'
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$src = Join-Path $PSScriptRoot 'mediamtx.exe'
if (-not (Test-Path $src)) {
    throw "mediamtx.exe nao encontrado em $PSScriptRoot. Baixe o build windows_amd64 em https://github.com/bluenviron/mediamtx/releases"
}
$exe = Join-Path $stage 'mediamtx.exe'
if (-not (Test-Path $exe) -or (Get-Item $src).Length -ne (Get-Item $exe).Length) {
    Copy-Item $src $exe -Force
}

Set-Location $stage
Write-Host "MediaMTX relay em rtsp://0.0.0.0:8554/camera (Ctrl+C para parar)"
& $exe
