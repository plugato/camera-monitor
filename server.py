#!/usr/bin/env python3
"""Monitor da câmera RTSP: página web com detecção de pessoas/animais.

Fluxo: FFmpeg puxa o RTSP da câmera e entrega MJPEG pelo pipe; este servidor
lê os frames, roda MobileNet-SSD, desenha as caixas, serve a página em
http://localhost:8090 e dispara uma notificação desktop.
"""

import hashlib
import hmac
import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections import deque
from email.header import Header
from urllib.parse import unquote, urlsplit

import cv2
import numpy as np

import config
import ptz
from flask import Flask, Response, jsonify, request, send_from_directory

# ---------------------------------------------------------------- config
# O backend pode ser escolhido com VIDEO_BACKEND=auto|ffmpeg|vlc.
FFMPEG_EXE = shutil.which("ffmpeg") or "ffmpeg"
FLATPAK_VLC = ([shutil.which("flatpak"), "run", "--command=cvlc",
                "org.videolan.VLC"] if shutil.which("flatpak") else None)
VLC_CANDIDATES = (
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    "/mnt/c/Program Files/VideoLAN/VLC/vlc.exe",
    shutil.which("vlc") or "",
)
VLC_EXE = next((path for path in VLC_CANDIDATES if os.path.exists(path)), None)
VIDEO_BACKEND = os.environ.get("VIDEO_BACKEND", "auto").lower()
MEDIAMTX_URL = os.environ.get("MEDIAMTX_URL", "rtsp://127.0.0.1:8554/camera")
if VIDEO_BACKEND == "auto":
    VIDEO_BACKEND = "mediamtx" if os.environ.get("MEDIAMTX_URL") else (
        "vlc" if (FLATPAK_VLC or VLC_EXE) else "ffmpeg")
RTSP_URL = config.RTSP_URL     # vem do .env, fora do git
CAM_HOST = config.CAM_HOST
HTTP_PORT = 8090
APP_USER = os.environ.get("APP_USER", "")
APP_PASS = os.environ.get("APP_PASS", "")
# Atrás do Cloudflare Access: com os dois definidos, o que chega pelo tunnel traz o
# JWT do header Cf-Access-Jwt-Assertion e é validado por ele; sem o header (acesso
# direto pela rede local) vale APP_USER/APP_PASS.
CF_ACCESS_TEAM = os.environ.get("CF_ACCESS_TEAM", "")  # <team>.cloudflareaccess.com
CF_ACCESS_AUD = os.environ.get("CF_ACCESS_AUD", "")    # Application Audience (AUD) tag
NTFY_URL = os.environ.get("NTFY_URL", "")  # push no celular: https://ntfy.sh/<tópico-secreto>
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # tocar no push abre o ao vivo
FOTOS_DIR = "fotos"            # snapshot anotado de cada notificação
# A detecção roda em thread própria sobre o frame mais recente, então o vídeo não
# trava enquanto ela pensa (num Pi 3 uma análise custa ~2,5s). Estes dois são o
# botão de CPU: pausa entre análises e threads do OpenCV (0 = todas).
DETECT_INTERVAL_S = float(os.environ.get("DETECT_INTERVAL_S") or "0.4")
DETECT_THREADS = int(os.environ.get("DETECT_THREADS") or "0")
DETECT_NICE = int(os.environ.get("DETECT_NICE") or "10")
# Quadros/s que o FFmpeg entrega. Cada um custa conversão, JPEG (se alguém
# assiste) e envio; a detecção usa ~1 a cada 4,5 s no Pi 3 e não sente.
VIDEO_FPS = int(os.environ.get("VIDEO_FPS") or "8")
# Só roda o MobileNet quando a cena muda: precisa de uma mancha contínua de
# mudança com MOTION_BLOB px (miniatura 160x90). Contar pixels soltos não
# servia: chuva muda 0,1-0,6% da cena em pingos de até 22 px e disparava
# análise sem parar; pessoa (até atrás da grade) dá mancha mediana de 263 px.
# 0 desliga o filtro. Parado, analisa mesmo assim a cada MOTION_IDLE_S para não
# perder quem ficou imóvel.
MOTION_BLOB = int(os.environ.get("MOTION_BLOB") or "40")
MOTION_IDLE_S = 10
# Pessoa atrás da grade do portão sai com 0,25-0,45 (barras cortam o corpo);
# portão vazio não dá nada nem no piso do modelo, 0,25 (medido de dia no Pi,
# 2026-10). Falso alarme à noite? Suba PESSOA_MIN. Animal fica alto: alucinação.
CONFIDENCE_MIN = {"pessoa": float(os.environ.get("PESSOA_MIN") or "0.25"),
                  "animal": 0.65}
NOTIFY_COOLDOWN_S = 20         # antispam de toast, por categoria
# nº de análises seguidas antes de notificar. No Pi 3 cada análise leva ~4,5s:
# com 3 para pessoa, quem para alguns segundos no portão nunca notificava. O 3
# fica para animal, que é onde o modelo alucina (carro/grade vira "pássaro").
STREAK_MIN = {"pessoa": 1, "animal": 3}
AUDIO_RATE = 16000             # SDP da câmera: a=rtpmap:8 PCMA/16000 (G.711 A-law)
STATIC_AFTER_S = 45            # caixa parada por mais que isso = cenário (ex.: carro
                               # atrás do portão que o modelo chama de "pássaro")
MAX_ANIMAL_AREA = 0.06         # animal ocupando mais que isso do frame é alucinação:
                               # o modelo chama carro/grade de "pássaro" com 80%.
                               # Calibrado p/ esta câmera (falsos medidos: 9%-65%);
                               # se um bicho real colado no portão for ignorado, suba.

VOC_CLASSES = ["background", "aeroplane", "bicycle", "bird", "boat", "bottle",
               "bus", "car", "cat", "chair", "cow", "diningtable", "dog",
               "horse", "motorbike", "person", "pottedplant", "sheep", "sofa",
               "train", "tvmonitor"]
LABELS_PT = {"person": "Pessoa", "bird": "Pássaro", "cat": "Gato",
             "cow": "Vaca", "dog": "Cachorro", "horse": "Cavalo",
             "sheep": "Ovelha"}
CATEGORY = {"person": "pessoa"} | {k: "animal" for k in
            ("bird", "cat", "cow", "dog", "horse", "sheep")}
BOX_COLOR = {"pessoa": (80, 180, 255), "animal": (120, 255, 120)}  # BGR

net = cv2.dnn.readNetFromCaffe("MobileNetSSD_deploy.prototxt",
                               "MobileNetSSD_deploy.caffemodel")
if DETECT_THREADS:
    cv2.setNumThreads(DETECT_THREADS)

app = Flask(__name__)
state = {
    "jpeg": None,               # último frame anotado (bytes)
    "detections": [],           # detecções do último frame analisado
    "events": deque(maxlen=50), # histórico de notificações
    "connected": False,
    "espectadores": 0,          # conexões abertas em /stream
    "last_notify": {},          # categoria -> timestamp
}
lock = threading.Lock()

if CF_ACCESS_TEAM and CF_ACCESS_AUD:
    import jwt
    cf_jwks = jwt.PyJWKClient(
        f"https://{CF_ACCESS_TEAM}.cloudflareaccess.com/cdn-cgi/access/certs")


def cf_access_ok():
    token = request.headers.get("Cf-Access-Jwt-Assertion", "")
    try:
        key = cf_jwks.get_signing_key_from_jwt(token).key
        jwt.decode(token, key, algorithms=["RS256"], audience=CF_ACCESS_AUD)
        return True
    except jwt.PyJWTError:
        return False


@app.before_request
def require_login():
    if request.path == "/healthz":
        return None
    if CF_ACCESS_TEAM and CF_ACCESS_AUD and "Cf-Access-Jwt-Assertion" in request.headers:
        return None if cf_access_ok() else ("acesso pelo Cloudflare Access", 403)
    auth = request.authorization
    valid = (auth is not None
             and hmac.compare_digest(auth.username, APP_USER)
             and hmac.compare_digest(auth.password, APP_PASS))
    if not APP_USER or not APP_PASS or not valid:
        return Response("autenticacao necessaria", 401,
                        {"WWW-Authenticate": 'Basic realm="Camera Monitor"'})


@app.route("/healthz")
def healthz():
    return "ok"


def phone_notify(title, msg, jpg=None):
    """Push via ntfy (app no celular), com a foto anexada. Em thread: rede lenta
    não pode segurar o loop de captura."""
    if not NTFY_URL:
        return
    headers = {"Title": Header(title, "utf-8").encode(),
               "Message": Header(msg, "utf-8").encode(),
               "Priority": "high", "Tags": "rotating_light"}
    if jpg:
        headers["Filename"] = "foto.jpg"
    if PUBLIC_URL:
        headers["Click"] = PUBLIC_URL
        headers["Actions"] = f"view, Ao vivo, {PUBLIC_URL}"

    def send():
        try:
            urllib.request.urlopen(urllib.request.Request(
                NTFY_URL, data=jpg or msg.encode(), headers=headers), timeout=15)
        except OSError as e:
            print(f"ntfy falhou: {e}", flush=True)
    threading.Thread(target=send, daemon=True).start()


def desktop_notify(title: str, msg: str) -> None:
    """Envia notificação nativa no Windows, Linux ou WSL."""
    if os.name == "nt":
        ps = ("Add-Type -AssemblyName System.Windows.Forms; "
              f"[System.Windows.Forms.MessageBox]::Show('{msg}', '{title}')")
        try:
            subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass
        return
    try:
        subprocess.Popen(["notify-send", title, msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def detect(frame):
    h, w = frame.shape[:2]
    blob = cv2.dnn.blobFromImage(cv2.resize(frame, (300, 300)),
                                 0.007843, (300, 300), 127.5)
    net.setInput(blob)
    out = net.forward()
    found = []
    for i in range(out.shape[2]):
        conf = float(out[0, 0, i, 2])
        cls = VOC_CLASSES[int(out[0, 0, i, 1])]
        if cls == "person" and 0.2 <= conf:   # no log: para calibrar o limiar
            print(f"pessoa {conf:.2f}{'' if conf >= CONFIDENCE_MIN['pessoa'] else ' (abaixo do limiar)'}",
                  flush=True)
        if cls not in CATEGORY or conf < CONFIDENCE_MIN[CATEGORY[cls]]:
            continue
        x1, y1, x2, y2 = out[0, 0, i, 3:7]
        if CATEGORY[cls] == "animal" and (x2 - x1) * (y2 - y1) > MAX_ANIMAL_AREA:
            continue
        box = out[0, 0, i, 3:7] * np.array([w, h, w, h])
        found.append({"label": LABELS_PT[cls], "category": CATEGORY[cls],
                      "confidence": round(conf, 2),
                      "box": box.astype(int).tolist()})
    return found


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area = lambda r: max(0, r[2] - r[0]) * max(0, r[3] - r[1])
    return inter / (area(a) + area(b) - inter or 1)


STALL_S = 10                   # sem frame novo por esse tempo -> reinicia o VLC
TRACK_GRACE_S = 60             # lembra a caixa por esse tempo se a detecção oscilar
                               # (>= STATIC_AFTER_S, senão o piscar zera o relógio)
tracked = []  # [box, first_seen, last_seen]


def drop_static(dets, now=None):
    """Descarta detecções cuja caixa está no mesmo lugar há mais de STATIC_AFTER_S.

    ponytail: um animal imóvel por >45s também some; suficiente para portão/rua.
    """
    global tracked
    now = time.time() if now is None else now
    new, keep = [], []
    for d in dets:
        hit = next((t for t in tracked if iou(t[0], d["box"]) > 0.6), None)
        since = hit[1] if hit else now
        if hit:
            tracked.remove(hit)
        new.append([d["box"], since, now])
        if now - since <= STATIC_AFTER_S:
            keep.append(d)
    tracked = new + [t for t in tracked if now - t[2] <= TRACK_GRACE_S]
    return keep


streaks = {}


def maybe_notify(dets):
    """Notifica só quando a categoria persiste por STREAK_MIN análises seguidas.

    Devolve os eventos disparados (sem foto ainda; capture_loop anexa)."""
    fired = []
    now = time.time()
    by_cat = {}
    for d in dets:
        by_cat.setdefault(d["category"], []).append(d["label"])
    for cat in list(streaks):
        if cat not in by_cat:
            streaks[cat] = 0
    for cat, labels in by_cat.items():
        streaks[cat] = streaks.get(cat, 0) + 1
        if streaks[cat] < STREAK_MIN[cat]:
            continue
        if now - state["last_notify"].get(cat, 0) < NOTIFY_COOLDOWN_S:
            continue
        state["last_notify"][cat] = now
        titulo = "Pessoa detectada!" if cat == "pessoa" else "Animal detectado!"
        msg = f"Câmera: {', '.join(sorted(set(labels)))} em cena."
        desktop_notify(titulo, msg)
        fired.append({"time": time.strftime("%H:%M:%S"), "title": titulo,
                      "msg": msg, "cat": cat})
    return fired


def kill_video(proc=None):
    """Encerra apenas o processo de vídeo iniciado pelo monitor."""
    if proc:
        proc.kill()


def vlc_frames():
    """Gera frames BGR pelo VLC ou FFmpeg, conforme a plataforma."""
    while True:
        if VIDEO_BACKEND == "vlc" and (FLATPAK_VLC or VLC_EXE):
            vlc_command = FLATPAK_VLC or [VLC_EXE]
            command = vlc_command + ["-I", "dummy", "--no-audio", "--rtsp-tcp",
                       RTSP_URL, f":sout=#transcode{{vcodec=MJPG,acodec=none,fps={VIDEO_FPS}}}"
                       ":standard{access=file,mux=mpjpeg,dst=-}"]
            use_vlc = True
        else:
            # Quadros crus (YUV4MPEG): sem codificar JPEG no FFmpeg só para o
            # Python decodificar de volta. O cabeçalho traz a resolução.
            src = MEDIAMTX_URL if VIDEO_BACKEND == "mediamtx" else RTSP_URL
            command = [FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
                       "-rtsp_transport", "tcp", "-i", src, "-map", "0:v:0", "-an",
                       "-vf", f"fps={VIDEO_FPS}", "-pix_fmt", "yuv420p",
                       "-f", "yuv4mpegpipe", "pipe:1"]
            use_vlc = False
        # mjpeg lê em pedaços (bufsize=0); y4m lê quadros de tamanho exato
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0 if use_vlc else -1)
        state["video_proc"] = proc
        last = [time.time()]

        def watchdog():  # o read() do pipe bloqueia se o backend travar
            caiu = 0
            while proc.poll() is None:
                caiu = caiu + 1 if conexao_caiu(proc.pid) else 0
                if time.time() - last[0] > STALL_S or caiu >= 3:
                    kill_video(proc)
                    return
                time.sleep(1)
        threading.Thread(target=watchdog, daemon=True).start()
        # Drena o pipe numa thread e guarda só o quadro mais novo: se o resto
        # não der conta dos 8 fps (Pi 3 com a detecção rodando), pula quadros em
        # vez de acumular atraso no pipe — já chegou a 30 min de vídeo velho.
        newest = queue.Queue(maxsize=1)

        def reader():
            for frame in (mjpeg_frames if use_vlc else y4m_frames)(proc.stdout):
                last[0] = time.time()
                put_newest(newest, frame)
            put_newest(newest, None)
        threading.Thread(target=reader, daemon=True).start()
        try:
            while (frame := newest.get()) is not None:
                state["connected"] = True
                yield frame if frame.ndim == 3 else cv2.cvtColor(
                    frame, cv2.COLOR_YUV2BGR_I420)
        finally:
            kill_video(proc)
        state["connected"] = False
        time.sleep(2)


ATRASO_BYTES = 64 * 1024        # ~1 s de H265 do substream parado no socket


def conexao_caiu(pid):
    """O FFmpeg ficou para trás? (Linux) Atrasado, ele segue soltando vídeo velho
    por minutos e quadros continuam chegando: só o STALL_S não percebe.

    Visto no Pi: o MediaMTX desiste da conexão, mas com o buffer de recepção
    cheio o FIN nem entra, e o lado do FFmpeg segue ESTABLISHED com 94 KB
    parados. Então vale tanto socket fora de ESTABLISHED quanto fila cheia."""
    inodes = set()
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                alvo = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if alvo.startswith("socket:["):
                inodes.add(alvo[8:-1])
        with open("/proc/net/tcp") as f:  # o FFmpeg está no mesmo netns
            socks = [(c[3], int(c[4].split(":")[1], 16))   # estado, rx_queue
                     for c in (l.split() for l in list(f)[1:]) if c[9] in inodes]
    except OSError:
        return False
    return bool(socks) and any(st != "01" or rx > ATRASO_BYTES  # 01 = ESTABLISHED
                               for st, rx in socks)


def mjpeg_frames(stream):
    buf = b""
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        buf = (buf + chunk)[-4_000_000:]
        while True:
            soi = buf.find(b"\xff\xd8")
            eoi = buf.find(b"\xff\xd9", soi + 2) if soi != -1 else -1
            if eoi == -1:
                break
            jpg, buf = buf[soi:eoi + 2], buf[eoi + 2:]
            frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                yield frame


def put_newest(q, item):
    """Põe item na fila de 1 lugar, descartando o que estiver lá (um produtor só)."""
    while True:
        try:
            return q.put_nowait(item)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass


def y4m_frames(stream):
    """Quadros I420 crus (h*3/2 x w) de um fluxo YUV4MPEG2; a conversão para BGR
    fica para quem consome, só nos quadros que não forem descartados."""
    header = stream.readline()
    if not header.startswith(b"YUV4MPEG2"):
        return
    params = {p[:1]: p[1:] for p in header.split()[1:]}
    w, h = int(params[b"W"]), int(params[b"H"])
    size = w * h * 3 // 2
    while stream.readline().startswith(b"FRAME"):
        data = stream.read(size)
        if len(data) < size:
            return
        yield np.frombuffer(data, np.uint8).reshape(h * 3 // 2, w)


detect_slot = {"frame": None, "dets": [], "fired": []}
detect_ready = threading.Event()


def motion_thumb(frame):
    small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)


def moved(ref, thumb):
    """Algo do tamanho de gente mudou desde a última análise? Diferença acima de
    20 (ruído de compressão fica abaixo), sem os pontos soltos (abertura 3x3,
    tira chuva) e com a maior mancha >= MOTION_BLOB."""
    if ref is None or MOTION_BLOB <= 0:
        return True
    mudou = (cv2.absdiff(ref, thumb) > 20).astype(np.uint8)
    mudou = cv2.morphologyEx(mudou, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mudou, connectivity=8)
    return n > 1 and stats[1:, cv2.CC_STAT_AREA].max() >= MOTION_BLOB


def detect_loop():
    # Prioridade baixa só nesta thread (Linux): se a CPU não der para tudo, quem
    # atrasa é a detecção, não o FFmpeg — senão ele fica para trás no H265 e o
    # MediaMTX derruba a sessão (vídeo congelado). Com DETECT_THREADS=1 o
    # MobileNet roda inteiro aqui; com mais, os workers do TBB ficam em nice 0.
    if hasattr(os, "setpriority"):
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), DETECT_NICE)
    ref, last_run = None, 0.0
    while True:
        detect_ready.wait()
        detect_ready.clear()
        with lock:
            frame = detect_slot["frame"]
        thumb = motion_thumb(frame)
        if not moved(ref, thumb) and time.time() - last_run < MOTION_IDLE_S:
            continue  # cena parada: não gasta o MobileNet
        ref, last_run = thumb, time.time()
        dets = drop_static(detect(frame))
        fired = maybe_notify(dets)  # sempre, para zerar streaks quando a cena esvazia
        with lock:
            detect_slot["dets"] = dets
            detect_slot["fired"] += fired
        time.sleep(DETECT_INTERVAL_S)


def capture_loop():
    for frame in vlc_frames():
        with lock:
            detect_slot["frame"] = frame.copy()  # o desenho abaixo não vaza pro detector
            dets, fired = detect_slot["dets"], detect_slot["fired"]
            detect_slot["fired"] = []
        detect_ready.set()
        if not fired and not state["espectadores"]:
            with lock:
                state["detections"] = dets
            continue  # ninguém vendo: sem desenho nem JPEG
        for d in dets:
            x1, y1, x2, y2 = d["box"]
            color = BOX_COLOR[d["category"]]
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, f'{d["label"]} {int(d["confidence"]*100)}%',
                        (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, color, 2)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            for ev in fired:  # foto já com as caixas desenhadas
                ev["photo"] = f"{time.strftime('%Y%m%d_%H%M%S')}_{ev.pop('cat')}.jpg"
                with open(os.path.join(FOTOS_DIR, ev["photo"]), "wb") as f:
                    f.write(jpg.tobytes())
                phone_notify(ev["title"], ev["msg"], jpg.tobytes())
            with lock:
                state["jpeg"] = jpg.tobytes()
                state["detections"] = dets
                for ev in fired:
                    state["events"].appendleft(ev)
            fired = []


# ------------------------------------------------------------ áudio
# O VLC não extrai o áudio dessa câmera e, pior, ocupa a única sessão de
# áudio que ela aceita. Então o áudio vem por um cliente RTSP próprio, via
# TCP intercalado (atravessa o NAT do WSL), e vai pro navegador como PCM16.
def _alaw_table():
    t = np.empty(256, np.int16)
    for i in range(256):
        a = i ^ 0x55
        v = (a & 0x0F) << 4
        seg = (a & 0x70) >> 4
        v = v + 8 if seg == 0 else (v + 0x108) << max(seg - 1, 0)
        t[i] = v if a & 0x80 else -v
    return t
ALAW = _alaw_table()
audio_clients = set()          # queue.Queue por cliente de /audio
audio_ready = threading.Event()
audio_thread_started = False
audio_thread_lock = threading.Lock()


def rtsp_audio():
    """Gera payloads PCMA (A-law) do track de áudio, reconectando sempre."""
    relay_audio = os.environ.get("AUDIO_RTSP_URL")
    url = relay_audio or RTSP_URL
    parsed = urlsplit(url)
    host, port = parsed.hostname, parsed.port or 554
    user = unquote(parsed.username or "")
    pwd = unquote(parsed.password or "")
    while True:
        s = None
        try:
            s = socket.create_connection((host, int(port)), timeout=5)
            cseq, auth, pend = [0], {}, [b""]

            def rx():  # recv() devolve b"" quando o outro lado fecha: sem isso o laço gira a 100% de CPU
                d = s.recv(65536)
                if not d:
                    raise OSError("RTSP: conexão fechada")
                return d

            def req(method, target, extra=""):
                cseq[0] += 1
                hdr = ""
                if auth:
                    ha1 = hashlib.md5(f"{user}:{auth['realm']}:{pwd}".encode()).hexdigest()
                    ha2 = hashlib.md5(f"{method}:{url}".encode()).hexdigest()
                    resp = hashlib.md5(f"{ha1}:{auth['nonce']}:{ha2}".encode()).hexdigest()
                    hdr = (f'Authorization: Digest username="{user}", realm="{auth["realm"]}", '
                           f'nonce="{auth["nonce"]}", uri="{url}", response="{resp}"\r\n')
                s.sendall(f"{method} {target} RTSP/1.0\r\nCSeq: {cseq[0]}\r\n{hdr}{extra}\r\n".encode())
                buf = pend[0]
                while True:  # pacotes $ intercalados podem vir antes da resposta
                    while len(buf) >= 4 and buf[0] == 0x24:
                        n = int.from_bytes(buf[2:4], "big")
                        if len(buf) < 4 + n:
                            break
                        buf = buf[4 + n:]
                    if buf.startswith(b"RTSP") and b"\r\n\r\n" in buf:
                        break
                    buf += rx()
                head, _, rest = buf.partition(b"\r\n\r\n")
                head = head.decode(errors="replace")
                m = re.search(r"Content-Length: (\d+)", head)
                n = int(m.group(1)) if m else 0
                while len(rest) < n:
                    rest += rx()
                pend[0] = rest[n:]
                return head

            h = req("DESCRIBE", url, "Accept: application/sdp\r\n")
            if " 401 " in h:
                auth = dict(re.findall(r'(realm|nonce)="([^"]+)"', h))
                h = req("DESCRIBE", url, "Accept: application/sdp\r\n")
            # A câmera usa track1/track2; o relay MediaMTX usa trackID=0/trackID=1.
            video_track = "/trackID=0" if relay_audio else "/track1"
            audio_track = "/trackID=1" if relay_audio else "/track2"
            h = req("SETUP", url + video_track,
                    "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
            sess = re.search(r"Session: ([^;\r\n]+)", h).group(1)
            h = req("SETUP", url + audio_track,
                    f"Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\nSession: {sess}\r\n")
            if " 200 " not in h.splitlines()[0]:
                proc = state.get("video_proc")   # VLC pegou a vaga de áudio: derruba, ele reinicia sem ela
                if proc and not relay_audio:     # no relay a falha é stream sem áudio (onvif2): não derruba o vídeo
                    kill_video(proc)
                raise OSError("SETUP áudio: " + h.splitlines()[0])
            req("PLAY", url, f"Session: {sess}\r\nRange: npt=0.000-\r\n")
            audio_ready.set()
            s.settimeout(STALL_S)
            buf, keep = pend[0], time.time()
            while True:
                if time.time() - keep > 30:  # sessão expira em 60 s sem requisição
                    keep = time.time()
                    s.sendall(f"OPTIONS {url} RTSP/1.0\r\nCSeq: 99\r\nSession: {sess}\r\n\r\n".encode())
                buf += rx()
                while True:
                    if buf.startswith(b"RTSP"):       # resposta do OPTIONS no meio do fluxo
                        i = buf.find(b"\r\n\r\n")
                        if i == -1:
                            break
                        buf = buf[i + 4:]
                        continue
                    if len(buf) < 4:
                        break
                    if buf[0] != 0x24:
                        buf = buf[1:]
                        continue
                    ch, n = buf[1], int.from_bytes(buf[2:4], "big")
                    if len(buf) < 4 + n:
                        break
                    pkt, buf = buf[4:4 + n], buf[4 + n:]
                    if ch == 2 and len(pkt) > 12:      # canal 2 = áudio; tira o cabeçalho RTP
                        hl = 12 + 4 * (pkt[0] & 0x0F)
                        if pkt[0] & 0x10:              # extensão
                            hl += 4 + 4 * int.from_bytes(pkt[hl + 2:hl + 4], "big")
                        yield pkt[hl:]
        except (OSError, AttributeError, IndexError):
            audio_ready.clear()
            time.sleep(3)
        finally:
            if s:
                s.close()


def audio_loop():
    for alaw in rtsp_audio():
        pcm = ALAW[np.frombuffer(alaw, np.uint8)].tobytes()
        for q in list(audio_clients):
            if q.qsize() < 200:            # cliente lento: descarta em vez de acumular atraso
                q.put(pcm)


@app.route("/audio")
def audio():
    """PCM16 LE mono, AUDIO_RATE Hz, sem cabeçalho; o navegador toca via Web Audio."""
    global audio_thread_started
    with audio_thread_lock:
        if not audio_thread_started:
            threading.Thread(target=audio_loop, daemon=True).start()
            audio_thread_started = True
    q = queue.Queue()
    audio_clients.add(q)

    def gen():
        try:
            while True:
                try:
                    yield q.get(timeout=1)
                except queue.Empty:  # sem áudio, manda silêncio: só escrevendo se percebe que o cliente saiu
                    yield bytes(AUDIO_RATE // 25)  # 20 ms de PCM16
        finally:
            audio_clients.discard(q)
    return Response(gen(), mimetype="application/octet-stream")


@app.route("/stream")
def stream():
    def gen():  # só quadro novo: repetir o mesmo JPEG pesava no cloudflared do Pi
        enviado = None
        with lock:
            state["espectadores"] += 1
        try:
            while True:
                with lock:
                    jpg = state["jpeg"]
                if jpg and jpg is not enviado:
                    enviado = jpg
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + jpg + b"\r\n")
                time.sleep(0.05)
        finally:  # cliente fechou: o servidor de WSGI fecha o gerador
            with lock:
                state["espectadores"] -= 1
    return Response(gen(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/fotos/<path:name>")
def foto(name):
    return send_from_directory(FOTOS_DIR, name)


@app.route("/status")
def status():
    with lock:
        return jsonify(connected=state["connected"],
                       detections=state["detections"],
                       events=list(state["events"]))


@app.route("/test")
def test():
    """Simula uma notificação (testa bipe/PiP/toast sem a câmera)."""
    fotos = sorted(os.listdir(FOTOS_DIR))
    ev = {"time": time.strftime("%H:%M:%S"), "title": "Teste!",
          "msg": "Notificação simulada.", "photo": fotos[-1] if fotos else None}
    desktop_notify("Teste", ev["msg"])
    phone_notify("Teste", ev["msg"])
    with lock:
        state["events"].appendleft(ev)
    return "ok, notificação simulada"


# ------------------------------------------------------------ PTZ e posições
# A câmera não informa onde está, e o passo é irregular (direita anda mais que
# esquerda). Então a posição é contada em px da miniatura 320x180 com o passo
# médio de cada direção, e cada posição salva guarda também a miniatura: ao
# voltar, anda pela conta e acerta o resto comparando a imagem (phaseCorrelate).
PRESETS_DIR = os.path.join(FOTOS_DIR, ".presets")  # oculto: fora do /test
PRESETS_JSON = os.path.join(PRESETS_DIR, "presets.json")
PASSO_PX = {"direita": 25, "esquerda": 15, "cima": 15, "baixo": 15}  # medido no Pi
VIDEO_ATRASO_S = 2.5           # o quadro que chega reflete o passo ~2s depois
ptz_lock = threading.Lock()
ptz_state = {"pos": [0.0, 0.0], "presets": {}, "indo": None}
try:
    with open(PRESETS_JSON) as f:
        ptz_state.update(json.load(f))
except (OSError, ValueError):
    pass


def ptz_salvar():
    os.makedirs(PRESETS_DIR, exist_ok=True)
    with open(PRESETS_JSON + ".tmp", "w") as f:
        json.dump({"pos": ptz_state["pos"], "presets": ptz_state["presets"]}, f)
    os.replace(PRESETS_JSON + ".tmp", PRESETS_JSON)


def ptz_passo(direcao):
    with ptz_lock:
        ptz.mover(direcao)
        x, y = ptz.VELOCIDADE[direcao]
        ptz_state["pos"][0] += x * PASSO_PX[direcao]
        ptz_state["pos"][1] += y * PASSO_PX[direcao]
        ptz_salvar()


def miniatura(frame):
    return cv2.cvtColor(cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA),
                        cv2.COLOR_BGR2GRAY).astype(np.float32)


def falta_andar(ref, atual):
    """(x, y, confiança): quanto a câmera precisa andar, em px da miniatura
    (+x = direita, +y = cima), para a imagem atual virar a de referência."""
    (dx, dy), conf = cv2.phaseCorrelate(ref, atual)
    return dx, -dy, conf


def rumo(ex, ey):
    """Direção do próximo passo, ou None se já está a menos de meio passo."""
    if abs(ex) > PASSO_PX["esquerda"] / 2:
        return "direita" if ex > 0 else "esquerda"
    if abs(ey) > PASSO_PX["cima"] / 2:
        return "cima" if ey > 0 else "baixo"
    return None


def ptz_ir(nome):
    alvo = ptz_state["presets"][nome]
    eu = object()
    ptz_state["indo"] = eu     # um movimento manual (ou outra ida) cancela esta
    for _ in range(80):        # grosso: pela posição contada
        pos = ptz_state["pos"]
        d = rumo(alvo["pos"][0] - pos[0], alvo["pos"][1] - pos[1])
        if d is None or ptz_state["indo"] is not eu:
            break
        ptz_passo(d)
    ref = cv2.imread(os.path.join(PRESETS_DIR, alvo["img"]), cv2.IMREAD_GRAYSCALE)
    for _ in range(10):        # fino: pela imagem salva
        time.sleep(VIDEO_ATRASO_S)
        with lock:
            frame = detect_slot["frame"]
        if ref is None or frame is None or ptz_state["indo"] is not eu:
            break
        ex, ey, conf = falta_andar(ref.astype(np.float32), miniatura(frame))
        if conf < 0.05:        # cena mudou demais (noite, chuva): fica na conta
            break
        ptz_state["pos"] = [alvo["pos"][0] - ex, alvo["pos"][1] - ey]  # recalibra
        d = rumo(ex, ey)
        if d is None:
            break
        ptz_passo(d)
    if ptz_state["indo"] is eu:
        ptz_state["indo"] = None


@app.route("/ptz/<direcao>", methods=["POST"])
def ptz_http(direcao):
    if direcao not in ptz.VELOCIDADE:
        return "direção inválida", 400
    ptz_state["indo"] = None
    ptz_passo(direcao)
    return "ok"


NOME_POSICAO = re.compile(r"[\w ,.-]{1,30}")  # vira texto de botão: sem < > & "


@app.route("/posicoes")
def posicoes():
    return jsonify(sorted(ptz_state["presets"]))


@app.route("/posicoes/<nome>", methods=["PUT", "POST", "DELETE"])
def posicao(nome):
    nome = nome.strip()
    if not NOME_POSICAO.fullmatch(nome):
        return "nome inválido", 400
    if request.method == "DELETE":
        alvo = ptz_state["presets"].pop(nome, None)
        if alvo:
            try:
                os.remove(os.path.join(PRESETS_DIR, alvo["img"]))
            except OSError:
                pass
            ptz_salvar()
        return "ok"
    if request.method == "POST":  # ir
        if nome not in ptz_state["presets"]:
            return "posição não existe", 404
        threading.Thread(target=ptz_ir, args=(nome,), daemon=True).start()
        return "ok"
    with lock:                    # PUT: salvar a posição atual
        frame = detect_slot["frame"]
    if frame is None:
        return "sem vídeo", 503
    img = hashlib.sha1(nome.encode()).hexdigest()[:12] + ".png"
    os.makedirs(PRESETS_DIR, exist_ok=True)
    cv2.imwrite(os.path.join(PRESETS_DIR, img), miniatura(frame).astype(np.uint8))
    ptz_state["presets"][nome] = {"pos": list(ptz_state["pos"]), "img": img}
    ptz_salvar()
    return "ok"


@app.route("/")
def index():
    return PAGE


PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monitor da Câmera</title><style>
  body{margin:0;font:14px system-ui,sans-serif;background:#111827;color:#e5e7eb}
  header{padding:14px 20px;background:#1f2937;display:flex;gap:12px;align-items:center}
  h1{font-size:16px;margin:0}
  #st{padding:3px 10px;border-radius:99px;font-size:12px;background:#7f1d1d}
  #st.on{background:#14532d}
  #snd,#pip,#au,#aual{background:#374151;color:#e5e7eb;border:0;border-radius:6px;padding:6px 12px;cursor:pointer}
  #snd{margin-left:auto}
  #snd.on,#au.on,#aual.on{background:#1d4ed8}
  main{display:grid;grid-template-columns:2fr 1fr;gap:16px;padding:16px;max-width:1300px;margin:auto}
  @media(max-width:900px){main{grid-template-columns:1fr}}
  img{width:100%;border-radius:8px;background:#000;min-height:240px}
  section{background:#1f2937;border-radius:8px;padding:14px}
  h2{font-size:13px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.05em;color:#9ca3af}
  .tag{display:inline-block;margin:0 6px 6px 0;padding:4px 10px;border-radius:99px;font-size:13px}
  .pessoa{background:#7c2d12}.animal{background:#14532d}
  ul{list-style:none;margin:0;padding:0;font-size:13px}
  li{padding:6px 0;border-bottom:1px solid #374151}
  li time{color:#9ca3af;margin-right:8px}
  .empty{color:#6b7280}
  .thumb{display:block;width:100%;border-radius:6px;margin-top:6px}
  #painel{background:#1f2937;border-radius:8px;padding:6px}
  .barra{display:flex;gap:6px;align-items:center;margin-bottom:6px}
  .barra button{background:#374151;color:#e5e7eb;border:0;border-radius:6px;
                padding:4px 9px;font-size:15px;line-height:1.1;cursor:pointer}
  .barra button.on{background:#1d4ed8}
  #st{width:10px;height:10px;padding:0;border-radius:50%;background:#7f1d1d;flex:none}
  #st.on{background:#22c55e}
  /* setas por cima da imagem: PTZ sem gastar altura */
  .cam{position:relative;line-height:0}
  .cam img{width:100%;border-radius:6px;display:block}
  .seta{position:absolute;top:50%;transform:translateY(-50%);border:0;cursor:pointer;
        background:rgba(17,24,39,.55);color:#e5e7eb;border-radius:8px;
        padding:16px 7px;font-size:15px;opacity:.35;transition:opacity .15s}
  .cam:hover .seta{opacity:.9}
  .seta:active{background:#1d4ed8}
  .seta{touch-action:none;user-select:none;-webkit-user-select:none}
  #pos{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
  #pos button{background:#374151;color:#e5e7eb;border:0;border-radius:6px;padding:4px 9px;cursor:pointer}
  #pos .x{padding:4px 6px;margin-left:-6px;border-radius:0 6px 6px 0;color:#9ca3af}
  #pos .ir{border-radius:6px 0 0 6px}
  .esq{left:6px} .dir{right:6px}
    .cima{top:6px;left:50%;transform:translateX(-50%)}
    .baixo{top:auto;bottom:6px;left:50%;transform:translateX(-50%)}
  #now{position:absolute;left:6px;right:6px;bottom:6px;line-height:1.5}
  #now:empty{display:none}
  #aviso{text-align:center;color:#9ca3af;padding:20px}
  body.so-pip header h1,body.so-pip .lista{display:none}
  body.so-pip main{grid-template-columns:1fr;padding:8px}
</style></head><body>
<header><h1>📷 Monitor da Câmera <span style="color:#9ca3af;font-weight:400">· Raspberry Pi</span></h1>
<button id="pip" title="Painel flutuante por cima de tudo, com todos os controles">🗗 PiP</button></header>
<main>
  <!-- #painel é movido inteiro para a janela de PiP: os controles vão junto, com
       os mesmos nós do DOM, então todos os handlers continuam valendo -->
  <div id="painel">
    <div class="barra"><span id="st" title="desconectado"></span>
      <button id="snd" title="bipe de alerta">🔕</button>
      <button id="au" title="áudio da câmera">🔇</button>
      <button id="aual" title="ligar o áudio sozinho por 20s quando houver detecção">🔈</button>
    </div>
        <div class="cam"><img src="/stream" alt="stream da câmera">
            <button class="seta cima" data-d="cima" title="inclinar para cima">▲</button>
      <button class="seta esq" data-d="esquerda" title="girar para a esquerda">◀</button>
      <button class="seta dir" data-d="direita" title="girar para a direita">▶</button>
            <button class="seta baixo" data-d="baixo" title="inclinar para baixo">▼</button>
      <div id="now"></div>
    </div>
    <div id="pos"></div>
  </div>
  <div class="lista">
    <section><h2>Notificações</h2>
      <ul id="log"><li class="empty">nenhuma ainda</li></ul></section>
  </div>
</main>
<p id="aviso" hidden>painel aberto na janela flutuante</p>
<script>
let ctx=null, lastEv=null;
// guardados uma vez: quando o painel vai para o PiP, document.getElementById
// daqui deixa de achá-los (eles passam a viver no documento da outra janela)
const painel=document.getElementById('painel'), elSt=document.getElementById('st'),
      elNow=document.getElementById('now'), elLog=document.getElementById('log');
const snd=document.getElementById('snd');
function setSnd(on){ snd.className=on?'on':''; snd.textContent=on?'🔔':'🔕';
  snd.title=on?'bipe de alerta ligado':'bipe de alerta desligado'; localStorage.setItem('snd',on?'1':'0'); }
snd.onclick=()=>{ const on=!ctx; if(on){ctx=new AudioContext(); beep();} else {ctx.close(); ctx=null;} setSnd(on); };
if(localStorage.getItem('snd')==='1'){ snd.title='clique para reativar o bipe'; }
// o navegador só libera áudio após um clique: qualquer clique religa o som se ele estava ligado
document.addEventListener('click',()=>{ if(!ctx && localStorage.getItem('snd')==='1'){ ctx=new AudioContext(); setSnd(true); } });
function beep(){ // 3 bipes, 880 Hz — o navegador só toca áudio depois de um clique
  if(!ctx) return;
  for(let i=0;i<3;i++){ const o=ctx.createOscillator(), g=ctx.createGain();
    o.connect(g); g.connect(ctx.destination); o.frequency.value=880;
    const t=ctx.currentTime+i*0.25; g.gain.setValueAtTime(0.3,t); g.gain.exponentialRampToValueAtTime(0.001,t+0.2);
    o.start(t); o.stop(t+0.2); }
}
async function tick(){
  try{
    const s = await (await fetch('/status')).json();
    const st = elSt;
    st.title = s.connected ? 'ao vivo' : 'desconectado';
    st.className = s.connected ? 'on' : '';
    elNow.innerHTML = s.detections.length
      ? s.detections.map(d=>`<span class="tag ${d.category}">${d.label} ${Math.round(d.confidence*100)}%</span>`).join('')
      : '';
    const newest = s.events[0] ? s.events[0].time + s.events[0].photo : null;
    if(lastEv !== null && newest && newest !== lastEv){ beep(); pipAlert(); audioNoAlerta(); }
    lastEv = newest || '';
    elLog.innerHTML = s.events.length
      ? s.events.map(e=>`<li><time>${e.time}</time>${e.title} — ${e.msg}${e.photo?`<a href="/fotos/${e.photo}" target="_blank"><img class="thumb" src="/fotos/${e.photo}" loading="lazy"></a>`:''}</li>`).join('')
      : '<li class="empty">nenhuma ainda</li>';
  }catch(e){}
}
// áudio da câmera: /audio manda PCM16 16 kHz; tocamos com Web Audio (fila de ~150 ms)
const au=document.getElementById('au'); let actx=null, auAbort=null;
function setAu(on){ au.className=on?'on':''; au.textContent=on?'🔊':'🔇';
  au.title=on?'áudio da câmera ligado':'áudio da câmera'; localStorage.setItem('cam',on?'1':'0'); }
async function camAudio(on){
  if(auAbort){ auAbort.abort(); auAbort=null; }
  if(!on) return;
  const ab=auAbort=new AbortController(); actx=actx||new AudioContext(); actx.resume();
  try{
    const rd=(await fetch('/audio',{signal:ab.signal})).body.getReader();
    let t=0, carry=new Uint8Array(0);
    for(;;){
      const {value,done}=await rd.read(); if(done) break;
      const b=new Uint8Array(carry.length+value.length); b.set(carry); b.set(value,carry.length);
      const n=b.length>>1, pcm=new Int16Array(b.buffer,0,n); carry=b.slice(n*2);
      if(!n) continue;
      const buf=actx.createBuffer(1,n,16000), f=buf.getChannelData(0);
      for(let i=0;i<n;i++) f[i]=pcm[i]/32768;
      const src=actx.createBufferSource(); src.buffer=buf; src.connect(actx.destination);
      if(t<actx.currentTime) t=actx.currentTime+0.15;
      src.start(t); t+=buf.duration;
    }
  }catch(e){}
  if(auAbort===ab){ setTimeout(()=>camAudio(true),2000); } // caiu sem ser desligado: reconecta
}
au.onclick=()=>{ const on=!auAbort; camAudio(on); setAu(on); porAlerta=false; };
// áudio no alerta: liga o som da câmera sozinho por ALERTA_S quando chega notificação
const ALERTA_S=20, aual=document.getElementById('aual');
let porAlerta=false, alertaTimer=null;
function setAual(on){ aual.className=on?'on':''; aual.textContent=on?'🔊':'🔈';
  aual.title=(on?'ligado: ':'desligado: ')+'áudio sozinho por 20s quando houver detecção';
  localStorage.setItem('aual',on?'1':'0'); }
aual.onclick=()=>{ const on=aual.className!=='on';
  if(on){ actx=actx||new AudioContext(); actx.resume(); }   // arma o contexto durante o clique
  setAual(on); };
if(localStorage.getItem('aual')==='1') setAual(true);
function audioNoAlerta(){
  if(localStorage.getItem('aual')!=='1') return;
  if(!auAbort){ camAudio(true); setAu(true); porAlerta=true; }   // não mexe se já está ligado na mão
  clearTimeout(alertaTimer);
  alertaTimer=setTimeout(()=>{ if(porAlerta){ camAudio(false); setAu(false); porAlerta=false; } }, ALERTA_S*1000);
}
if(localStorage.getItem('cam')==='1'){ au.title='clique para reativar o áudio'; }
document.addEventListener('click',()=>{ if(!auAbort && localStorage.getItem('cam')==='1'){ camAudio(true); setAu(true); }
  if(localStorage.getItem('aual')==='1'){ actx=actx||new AudioContext(); actx.resume(); } });
// PTZ: segurar a seta anda sem parar (um passo por vez, ~0,75s cada); um
// clique é um passo. No PiP, os controles de mídia ⏮ ⏭ (lá não há DOM).
let segurando=null;
async function girar(d){
  segurando=d;
  do{ try{ await fetch('/ptz/'+d,{method:'POST'}); }catch(e){ break; } }while(segurando===d);
}
function passo(d){ girar(d); segurando=null; }
painel.querySelectorAll('.seta').forEach(b=>{
  b.onpointerdown=e=>{ e.preventDefault(); girar(b.dataset.d); };
  b.onpointerup=b.onpointerleave=b.onpointercancel=()=>{ segurando=null; };
  b.oncontextmenu=e=>e.preventDefault();   // segurar no celular não abre menu
});
// posições salvas: ir, apagar (×) e salvar a atual (＋)
const elPos=document.getElementById('pos');
async function posicoes(){
  try{
    const l=await (await fetch('/posicoes')).json();
    elPos.innerHTML=l.map(n=>`<span><button class="ir" data-n="${n}" title="ir para ${n}">📍 ${n}</button><button class="x" data-n="${n}" title="apagar ${n}">×</button></span>`).join('')
      +'<button id="salvar" title="salvar a posição atual da câmera">＋ posição</button>';
  }catch(e){}
}
elPos.onclick=async e=>{
  const b=e.target.closest('button'); if(!b) return;
  const url=n=>'/posicoes/'+encodeURIComponent(n);
  if(b.id==='salvar'){
    const n=(prompt('Nome da posição (ex.: portão, garagem):')||'').trim(); if(!n) return;
    const r=await fetch(url(n),{method:'PUT'}); if(!r.ok) alert(await r.text());
  }else if(b.classList.contains('x')){
    if(!confirm('Apagar a posição "'+b.dataset.n+'"?')) return;
    await fetch(url(b.dataset.n),{method:'DELETE'});
  }else{ segurando=null; await fetch(url(b.dataset.n),{method:'POST'}); }
  posicoes();
};
posicoes();
if('mediaSession' in navigator){
  navigator.mediaSession.metadata=new MediaMetadata({title:'Monitor da Câmera',artist:'⏮ ⏭ giram a câmera'});
  navigator.mediaSession.setActionHandler('previoustrack',()=>passo('esquerda'));
  navigator.mediaSession.setActionHandler('nexttrack',()=>passo('direita'));
}
// PiP: janela flutuante com o painel inteiro dentro (Document PiP).
// Movemos o nó real do painel para lá e o trazemos de volta ao fechar.
const pip=document.getElementById('pip');
let pipWin=null;
pip.onclick=async()=>{
  if(pipWin){ pipWin.close(); return; }
  if(!window.documentPictureInPicture){
    alert('Esta janela flutuante precisa de Chrome ou Edge 116+.'); return; }
  pipWin=await documentPictureInPicture.requestWindow({width:480,height:330,disallowReturnToOpener:true});
  document.querySelectorAll('style').forEach(e=>pipWin.document.head.append(e.cloneNode(true)));
  const extra=pipWin.document.createElement('style');
  extra.textContent='body{margin:0;background:#111827}#painel{border-radius:0;min-height:100vh}';
  pipWin.document.head.append(extra);
  pipWin.document.body.append(painel);        // move o painel de verdade
  document.getElementById('aviso').hidden=false;
  pipWin.addEventListener('pagehide',()=>{
    document.querySelector('main').prepend(painel);   // devolve para a página
    document.getElementById('aviso').hidden=true; pipWin=null;
  });
};
// modo "só o painel": aberto pelo abrir-pip.sh, entra em PiP no primeiro clique
if(location.search.includes('pip=1')){
  document.body.classList.add('so-pip');
  document.addEventListener('click',()=>{ if(!pipWin) pip.click(); },{once:true});
}
function pipAlert(){ // pisca a borda do painel ~2,4s, esteja ele na página ou no PiP
  painel.style.outline='4px solid #ef4444';
  setTimeout(()=>{ painel.style.outline=''; },2400); }
setInterval(tick, 1500); tick();
</script></body></html>"""


def _selfcheck():
    global tracked
    tracked = []
    car = {"box": [0, 60, 830, 540], "category": "animal", "label": "Pássaro"}
    cat = {"box": [900, 700, 1000, 800], "category": "animal", "label": "Gato"}
    assert drop_static([car], now=0) == [car]            # novo: passa
    assert drop_static([car], now=STATIC_AFTER_S + 1) == []  # parado: cai
    assert drop_static([car, cat], now=STATIC_AFTER_S + 2) == [cat]  # gato novo passa
    assert drop_static([], now=STATIC_AFTER_S + 3) == []             # carro oscilou
    assert drop_static([car], now=STATIC_AFTER_S + 10) == []         # continua parado
    tracked = []
    y4m = io.BytesIO(b"YUV4MPEG2 W4 H2 F8:1 Ip A1:1 C420jpeg\n"
                     + (b"FRAME\n" + bytes(12)) * 2 + b"FRAME\n" + bytes(5))
    assert [f.shape for f in y4m_frames(y4m)] == [(3, 4), (3, 4)]  # truncado cai
    q = queue.Queue(maxsize=1)
    for i in range(5):                                               # consumidor lento
        put_newest(q, i)
    assert q.get_nowait() == 4 and q.empty()                         # só o mais novo
    still = np.full((360, 640, 3), 90, np.uint8)
    walk = still.copy()
    walk[150:250, 300:340] = 240                                     # alguém entrou
    assert moved(None, motion_thumb(still))
    assert not moved(motion_thumb(still), motion_thumb(still))
    assert moved(motion_thumb(still), motion_thumb(walk))
    chuva = still.copy()
    for y, x in np.random.default_rng(2).integers(0, (356, 636), (300, 2)):
        chuva[y:y + 4, x:x + 2] = 250                                # pingos espalhados
    assert not moved(motion_thumb(still), motion_thumb(chuva))
    big = np.random.default_rng(1).random((400, 700)).astype(np.float32)
    big = cv2.GaussianBlur(big, (0, 0), 3)
    ref = big[50:230, 100:420]                                       # posição salva
    ex, ey, conf = falta_andar(ref, big[50:230, 120:440])            # câmera 20px à direita
    assert abs(ex + 20) < 1 and abs(ey) < 1 and conf > 0.3 and rumo(ex, ey) == "esquerda"
    ex, ey, _ = falta_andar(ref, big[60:240, 100:420])               # 10px abaixo
    assert abs(ey - 10) < 1 and rumo(ex, ey) == "cima"
    assert rumo(*falta_andar(ref, ref)[:2]) is None                  # já está lá
    assert ALAW[0x55] == -8 and ALAW[0xD5] == 8 and ALAW[0x2A] == -32256 and ALAW[0xAA] == 32256


def start_capture_worker():
    """Inicia a captura uma vez, tanto no Gunicorn quanto no modo local."""
    if not any(thread.name == "camera-capture" for thread in threading.enumerate()):
        threading.Thread(target=capture_loop, name="camera-capture", daemon=True).start()
        threading.Thread(target=detect_loop, name="camera-detect", daemon=True).start()


if __name__ == "__main__":
    _selfcheck()
    os.makedirs(FOTOS_DIR, exist_ok=True)
    start_capture_worker()
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)
