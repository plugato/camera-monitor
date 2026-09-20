#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

docker compose up -d --build
echo "Monitor no ar: http://localhost:8090"
