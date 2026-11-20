#!/bin/sh
# Comando forcado da chave de deploy do GitHub no Raspberry Pi: le um tar.gz no
# stdin com os arquivos do app, extrai e sobe. A chave nao ganha shell nenhum.
# Instalacao (uma vez, no Pi):
#   install -m755 deploy-pi.sh ~/bin/camera-deploy
#   ~/.ssh/authorized_keys:
#   command="$HOME/bin/camera-deploy",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... github-deploy
set -eu
cd "$HOME/camera-monitor"
tar -xzf -
# Base arm64 (pacotes, OpenCV, MediaMTX): o build do Pi nao roda RUN arm64, entao ela
# sai do base-pi.sh. So na primeira vez; rode o script na mao para atualizar a base.
[ -n "$(docker images -q camera-monitor-base:arm64)" ] || sh base-pi.sh
docker compose -f compose.pi.yaml up -d --build --wait --wait-timeout 300
docker image prune -f >/dev/null
docker compose -f compose.pi.yaml ps --format '{{.Name}} {{.Status}}'
