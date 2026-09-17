#!/usr/bin/env bash
# Sobe o monitor da câmera: mata instâncias antigas e inicia o servidor.
# O servidor lança o vlc.exe sozinho (relay RTSP -> MJPEG via pipe).
cd "$(dirname "$0")"
pkill -f 'python server.py' 2>/dev/null
/mnt/c/Windows/System32/taskkill.exe /IM vlc.exe /F 2>/dev/null
sleep 1
nohup ./.venv/bin/python server.py > server.log 2>&1 &
echo "Monitor no ar: http://localhost:8090"
