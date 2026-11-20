#!/bin/sh
# Monta a imagem base arm64 do Pi (camera-monitor-base:arm64): pacotes do sistema,
# OpenCV e MediaMTX. O Dockerfile.pi parte dela e so copia o app.
#
# Por que nao no Dockerfile: o Pi roda kernel 64 bits com Docker armhf (sistema 32
# bits). Container arm64 roda com o seccomp-pi.json, mas o RUN do build nao aceita
# perfil seccomp e morre com SIGSYS. Entao a base sai de um container comum + commit.
#
# Rode de novo para pegar atualizacoes de seguranca do Debian; o proximo deploy usa.
# O deploy (deploy-pi.sh) chama este script sozinho se a base nao existir.
set -eu
cd "$(dirname "$0")"

TAG=camera-monitor-base:arm64
MEDIAMTX_VERSION=v1.21.1
# OpenCV oficial do PyPI: compilado com NEON, ~0,84 s por analise do MobileNet no
# Pi 3 contra ~1,25 s do python3-opencv do Debian arm64 e ~4,3 s do armhf.
CV_WHL=opencv_python_headless-4.10.0.84-cp37-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
CV_SHA=46071015ff9ab40fccd8a163da0ee14ce9846349f06c6c8c0f2870856ffa45db
CV_URL="https://files.pythonhosted.org/packages/91/61/f838ce2046f3ec3591ea59ea3549085e399525d3b4558c4ed60b55ed88c0/opencv_python_headless-4.10.0.84-cp37-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl"

# A tag debian:bookworm-slim guarda uma arquitetura so neste Docker: usa o ID.
docker pull -q --platform linux/arm64 debian:bookworm-slim >/dev/null
BASE=$(docker image inspect debian:bookworm-slim --format '{{.Id}}')
[ "$(docker image inspect "$BASE" --format '{{.Architecture}}')" = arm64 ] \
    || { echo "base debian nao e arm64" >&2; exit 1; }

NOME=camera-monitor-base-build
docker rm -f "$NOME" >/dev/null 2>&1 || true
docker run --name "$NOME" --platform linux/arm64 --security-opt seccomp=seccomp-pi.json \
    -e CV_WHL="$CV_WHL" -e CV_SHA="$CV_SHA" -e CV_URL="$CV_URL" -e MEDIAMTX_VERSION="$MEDIAMTX_VERSION" \
    "$BASE" sh -euc '
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl \
        python3 python3-numpy python3-flask python3-jwt python3-cryptography gunicorn
    curl -fsSL -o "/tmp/$CV_WHL" "$CV_URL"
    echo "$CV_SHA  /tmp/$CV_WHL" | sha256sum -c -
    python3 -m zipfile -e "/tmp/$CV_WHL" /usr/local/lib/python3.11/dist-packages/
    rm "/tmp/$CV_WHL"
    curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_arm64.tar.gz" \
        | tar -xz -C /usr/local/bin mediamtx
    python3 -c "import cv2, numpy; print(\"cv2\", cv2.__version__, \"numpy\", numpy.__version__)"
    rm -rf /var/lib/apt/lists/*
'
docker commit --change 'CMD []' --change 'ENTRYPOINT []' "$NOME" "$TAG" >/dev/null
docker rm "$NOME" >/dev/null
docker image inspect "$TAG" --format "$TAG pronta: {{.Architecture}}, {{.Size}} bytes"
