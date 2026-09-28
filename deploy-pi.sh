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
docker compose -f compose.pi.yaml up -d --build --wait --wait-timeout 300
docker image prune -f >/dev/null
docker compose -f compose.pi.yaml ps --format '{{.Name}} {{.Status}}'
