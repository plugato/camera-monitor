#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

RTSP_URL="$(python3 -c 'import config; print(config.RTSP_URL)')"
export MTX_PATHS_CAMERA_SOURCE="$RTSP_URL"
exec "$HOME/.local/bin/mediamtx" mediamtx.yml