# Changelog

## Unreleased

- Deteccao so roda quando a cena muda (`MOTION_MIN`), com analise forcada a cada 10 s.
- FFmpeg entrega quadros crus (YUV4MPEG) em vez de MJPEG: ~3x menos CPU no Pi 3.
- Adicionado deploy automatico no Raspberry Pi a cada push na `main` (job `deploy-pi`).
- Corrigido o CI: `Validate Compose` falhava sem `.env` no runner.
- Adicionado deploy no Raspberry Pi 3 (`Dockerfile.pi`, `compose.pi.yaml`), sem PC ligado.
- Adicionado push no celular via ntfy, com foto do alerta e link para o ao vivo.
- Deteccao passou a rodar em thread propria: o video nao trava durante a analise.
- Adicionados `DETECT_THREADS` e `DETECT_INTERVAL_S` para ajustar o custo de CPU.
- Adicionado `CAM_AUDIO_PATH` para puxar o audio de outro caminho quando o substream nao tem audio.
- Corrigido: falha de audio no relay derrubava o video.
- Gunicorn com 8 threads: varios espectadores nao travam o painel.
- Removido do Git o certificado autoassinado gerado pelo MediaMTX (`auto.crt`/`auto.key`).
- Adicionada documentacao visual e guia completo de operacao.
- Adicionado CI para Python, shell, Compose e build Docker.
- Adicionado Gunicorn para servir a aplicacao em container.
- Fixada a imagem do Cloudflare Tunnel.
- Adicionado movimento PTZ vertical para cima e para baixo.
- Adicionada autenticacao HTTP Basic no painel.
