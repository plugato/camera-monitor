# Changelog

## Unreleased

- Push no ntfy quando o video para por 1 min, quando volta, e quando o watchdog reinicia o FFmpeg 3+ vezes em 10 min.
- Container do Pi em `America/Sao_Paulo`: hora das notificacoes e nome das fotos saiam em UTC (3 h a frente).
- Watchdog tambem reinicia o FFmpeg com mais de 64 KB parados no socket: atrasado, o lado dele seguia ESTABLISHED e o painel travava.
- `VIDEO_FPS` (4 no Pi): metade do custo de conversao, JPEG e envio.
- Sem ninguem no `/stream`, a captura nao gera JPEG (so a foto do alerta).
- Filtro de movimento ignora chuva: exige mancha continua de `MOTION_BLOB` px (substitui `MOTION_MIN`). Na chuva o MobileNet rodava sem parar.
- Watchdog reinicia o FFmpeg quando o MediaMTX derruba a conexao dele (antes o painel ficava minutos em video velho).
- `/stream` so envia quadro novo: mandava 12,5 JPEG/s para 8 quadros/s, carga a mais no cloudflared do Pi.
- Limiar de pessoa 0,65 -> 0,25 (`PESSOA_MIN`): atras da grade do portao a confianca fica em 0,25-0,45; no video do entregador, de 7% para 66% dos quadros detectados.
- Pessoa notifica na 1a analise (animal continua em 3 seguidas): no Pi cada analise leva ~4,5 s e quem parava poucos segundos no portao nao gerava alerta. Pessoas detectadas e quase-detectadas (0,4 a 0,65) vao para o log.
- PTZ mais rapido: sem o `Stop` e sem a espera de 3 s; segurar a seta anda sem parar.
- Adicionadas posicoes salvas da camera (`/posicoes`), com ajuste final pela imagem.
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
