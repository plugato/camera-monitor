# Seguranca

## Comunicacao de vulnerabilidades

Nao publique credenciais da camera, tokens Cloudflare, fotos ou logs em issues e pull requests.

Para relatar uma vulnerabilidade, abra um contato privado com o mantenedor do repositorio. Inclua:

- impacto observado;
- passos para reproduzir;
- versao ou commit afetado;
- sugestao de mitigacao, se houver.

## Cuidados operacionais

- Mantenha `.env` fora do Git.
- Use uma senha forte em `APP_PASS`.
- Revogue tokens que tenham sido compartilhados.
- Restrinja o dominio remoto usando Cloudflare Access.
- Nao exponha diretamente as portas RTSP ou ONVIF na internet.
