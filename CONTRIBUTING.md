# Contribuindo

## Fluxo local

1. Crie uma branch para a mudanca.
2. Copie `.env.exemplo` para `.env` e preencha os valores locais.
3. Rode as validacoes antes de abrir o pull request.
4. Descreva comportamento alterado, riscos e como testar.

## Validacoes

```bash
python3 -m py_compile server.py config.py ptz.py
bash -n start.sh start-mediamtx.sh docker-entrypoint.sh
docker compose config --quiet
docker build -t camera-monitor:local .
```

Nao inclua credenciais, fotos da camera, logs, tokens ou arquivos gerados no commit.
