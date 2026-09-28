#!/bin/sh
set -eu

# No Linux o conntrack do netfilter devolve o RTP/UDP da camera para dentro do
# container, entao o MediaMTX sobe aqui mesmo. Em WSL2 existe um NAT a mais, do
# lado do Windows, que nao tem esse retorno: o handshake RTSP passa e a midia
# nunca chega. La o MediaMTX roda no host Windows, que enxerga a camera na mesma
# sub-rede, e aqui so consumimos o relay dele por TCP.
if [ -n "${EXTERNAL_MEDIAMTX_URL:-}" ]; then
    export MEDIAMTX_URL="$EXTERNAL_MEDIAMTX_URL"
    export AUDIO_RTSP_URL="$MEDIAMTX_URL"
    exec gunicorn -c gunicorn.conf.py server:app
fi

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
    # Substream sem audio (onvif2): o audio vem de outro caminho da camera, e so
    # e puxado enquanto alguem escuta.
    audio_path = os.environ.get('CAM_AUDIO_PATH', '').lstrip('/')
    if audio_path:
        config.write('  audio:\n')
        config.write(f'    source: rtsp://{user}:{password}@{host}:{port}/{audio_path}\n')
        config.write('    rtspTransport: udp\n    sourceOnDemand: yes\n')
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
if [ -n "${CAM_AUDIO_PATH:-}" ]; then
    export AUDIO_RTSP_URL="rtsp://127.0.0.1:8554/audio"
fi
exec gunicorn -c gunicorn.conf.py server:app
