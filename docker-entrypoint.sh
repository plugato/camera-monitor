#!/bin/sh
set -eu

python3 - <<'PY'
import os
from urllib.parse import quote

user = quote(os.environ['CAM_USER'], safe='')
password = quote(os.environ['CAM_PASS'], safe='')
host = os.environ['CAM_HOST']
port = os.environ.get('CAM_RTSP_PORT', '554')
path = os.environ.get('CAM_PATH', 'onvif1').lstrip('/')
url = f'rtsp://{user}:{password}@{host}:{port}/{path}'
with open('/tmp/mediamtx.yml', 'w', encoding='ascii') as config:
    config.write('logLevel: info\npaths:\n  camera:\n')
    config.write(f'    source: {url}\n')
    config.write('    rtspTransport: udp\n')
PY

/usr/local/bin/mediamtx /tmp/mediamtx.yml > /tmp/mediamtx.log 2>&1 &
mediamtx_pid=$!
trap 'kill "$mediamtx_pid" 2>/dev/null || true' TERM INT EXIT

for _ in $(seq 1 30); do
    if grep -q 'stream is available and online' /tmp/mediamtx.log; then
        break
    fi
    if ! kill -0 "$mediamtx_pid" 2>/dev/null; then
        cat /tmp/mediamtx.log >&2
        exit 1
    fi
    sleep 1
done

export MEDIAMTX_URL="rtsp://127.0.0.1:8554/camera"
export AUDIO_RTSP_URL="$MEDIAMTX_URL"
exec gunicorn -c gunicorn.conf.py server:app
