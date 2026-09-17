#!/usr/bin/env python3
"""Monitor da câmera RTSP: página web com detecção de pessoas/animais.

Fluxo: VLC (Windows) puxa o RTSP da câmera via UDP e re-serve como MJPEG
por HTTP; este servidor lê o MJPEG, roda MobileNet-SSD nos frames, desenha
as caixas, serve a página em http://localhost:8090 e dispara toast nativo
do Windows (via powershell.exe) quando pessoa ou animal aparece.
"""

import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
from collections import deque

import cv2
import numpy as np

import config
import ptz
from flask import Flask, Response, jsonify, send_from_directory

# ---------------------------------------------------------------- config
# O WSL (modo NAT) não recebe o RTP/UDP da câmera e o firewall do Windows
# bloqueia portas abertas no host, então o vlc.exe roda como subprocesso e
# entrega o MJPEG pelo stdout (pipe atravessa a fronteira WSL/Windows).
VLC_EXE = "/mnt/c/Program Files/VideoLAN/VLC/vlc.exe"
RTSP_URL = config.RTSP_URL     # vem do .env, fora do git
VLC_SOUT = (":sout=#transcode{vcodec=MJPG,fps=8}"
            ":standard{access=file,mux=mpjpeg,dst=-}")
CAM_HOST = config.CAM_HOST
HTTP_PORT = 8090
FOTOS_DIR = "fotos"            # snapshot anotado de cada notificação
DETECT_EVERY_N_FRAMES = 3      # detecção a cada N frames (CPU)
CONFIDENCE_MIN = 0.65
NOTIFY_COOLDOWN_S = 20         # antispam de toast, por categoria
STREAK_MIN = 3                 # nº de análises consecutivas antes de notificar
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

app = Flask(__name__)
state = {
    "jpeg": None,               # último frame anotado (bytes)
    "detections": [],           # detecções do último frame analisado
    "events": deque(maxlen=50), # histórico de notificações
    "connected": False,
    "last_notify": {},          # categoria -> timestamp
}
lock = threading.Lock()


def windows_toast(title: str, msg: str) -> None:
    """Toast nativo do Windows via WinRT (Windows PowerShell 5.1)."""
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode('{title}')) | Out-Null
$t.Item(1).AppendChild($xml.CreateTextNode('{msg}')) | Out-Null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Camera Monitor').Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        if conf < CONFIDENCE_MIN or cls not in CATEGORY:
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
        if streaks[cat] < STREAK_MIN:
            continue
        if now - state["last_notify"].get(cat, 0) < NOTIFY_COOLDOWN_S:
            continue
        state["last_notify"][cat] = now
        titulo = "Pessoa detectada!" if cat == "pessoa" else "Animal detectado!"
        msg = f"Câmera: {', '.join(sorted(set(labels)))} em cena."
        windows_toast(titulo, msg)
        fired.append({"time": time.strftime("%H:%M:%S"), "title": titulo,
                      "msg": msg, "cat": cat})
    return fired


def kill_vlc(proc=None):
    """proc.kill() derruba só o wrapper do WSL: o vlc.exe do Windows sobrevive e
    segura a sessão RTSP (a câmera só aceita uma), então o VLC novo nunca pega o
    stream -> trava -> reinicia -> vaza outro. Mata pela linha de comando, para
    não derrubar um VLC que o usuário tenha aberto para outra coisa."""
    if proc:
        proc.kill()
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='vlc.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -like '*{CAM_HOST}*' }} | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def vlc_frames():
    """Gera frames BGR lendo o MJPEG do stdout do vlc.exe (Windows)."""
    while True:
        proc = subprocess.Popen(
            [VLC_EXE, "-I", "dummy", "--no-audio", RTSP_URL, VLC_SOUT],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        state["video_proc"] = proc
        last = [time.time()]

        def watchdog():  # o read() do pipe bloqueia para sempre se o VLC travar
            while proc.poll() is None:
                if time.time() - last[0] > STALL_S:
                    kill_vlc(proc)
                    return
                time.sleep(1)
        threading.Thread(target=watchdog, daemon=True).start()
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf = (buf + chunk)[-4_000_000:]
                while True:
                    soi = buf.find(b"\xff\xd8")
                    eoi = buf.find(b"\xff\xd9", soi + 2) if soi != -1 else -1
                    if eoi == -1:
                        break
                    jpg, buf = buf[soi:eoi + 2], buf[eoi + 2:]
                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8),
                                         cv2.IMREAD_COLOR)
                    if frame is not None:
                        last[0] = time.time()
                        state["connected"] = True
                        yield frame
        finally:
            kill_vlc(proc)
        state["connected"] = False
        time.sleep(2)


def capture_loop():
    n = 0
    dets, fired = [], []
    for frame in vlc_frames():
        n += 1
        if n % DETECT_EVERY_N_FRAMES == 0:
            dets = drop_static(detect(frame))
            fired = maybe_notify(dets)  # sempre, para zerar streaks quando a cena esvazia
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


def rtsp_audio():
    """Gera payloads PCMA (A-law) do track de áudio, reconectando sempre."""
    host, port = config.CAM_HOST, config.CAM_RTSP_PORT
    user, pwd = config.CAM_USER, config.CAM_PASS
    url = f"rtsp://{host}:{port}/{config.CAM_PATH}"
    while True:
        s = None
        try:
            s = socket.create_connection((host, int(port)), timeout=5)
            cseq, auth, pend = [0], {}, [b""]

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
                    buf += s.recv(65536)
                head, _, rest = buf.partition(b"\r\n\r\n")
                head = head.decode(errors="replace")
                m = re.search(r"Content-Length: (\d+)", head)
                n = int(m.group(1)) if m else 0
                while len(rest) < n:
                    rest += s.recv(65536)
                pend[0] = rest[n:]
                return head

            h = req("DESCRIBE", url, "Accept: application/sdp\r\n")
            if " 401 " in h:
                auth = dict(re.findall(r'(realm|nonce)="([^"]+)"', h))
                h = req("DESCRIBE", url, "Accept: application/sdp\r\n")
            # a câmera exige o vídeo na sessão (SETUP só do áudio falha); descartamos o canal 0
            h = req("SETUP", url + "/track1", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
            sess = re.search(r"Session: ([^;\r\n]+)", h).group(1)
            h = req("SETUP", url + "/track2",
                    f"Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\nSession: {sess}\r\n")
            if " 200 " not in h.splitlines()[0]:
                proc = state.get("video_proc")   # VLC pegou a vaga de áudio: derruba, ele reinicia sem ela
                if proc:
                    kill_vlc(proc)
                raise OSError("SETUP áudio: " + h.splitlines()[0])
            req("PLAY", url, f"Session: {sess}\r\nRange: npt=0.000-\r\n")
            audio_ready.set()
            s.settimeout(STALL_S)
            buf, keep = pend[0], time.time()
            while True:
                if time.time() - keep > 30:  # sessão expira em 60 s sem requisição
                    keep = time.time()
                    s.sendall(f"OPTIONS {url} RTSP/1.0\r\nCSeq: 99\r\nSession: {sess}\r\n\r\n".encode())
                buf += s.recv(65536)
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
    q = queue.Queue()
    audio_clients.add(q)

    def gen():
        try:
            while True:
                yield q.get()
        finally:
            audio_clients.discard(q)
    return Response(gen(), mimetype="application/octet-stream")


@app.route("/stream")
def stream():
    def gen():
        while True:
            with lock:
                jpg = state["jpeg"]
            if jpg:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + jpg + b"\r\n")
            time.sleep(0.08)
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
    windows_toast("Teste", ev["msg"])
    with lock:
        state["events"].appendleft(ev)
    return "ok, notificação simulada"


@app.route("/ptz/<direcao>", methods=["POST"])
def ptz_http(direcao):
    if direcao not in ptz.VELOCIDADE:
        return "direção inválida", 400
    ptz.mover(direcao)
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
  .seta:disabled{opacity:.15;cursor:wait}
  .esq{left:6px} .dir{right:6px}
  #now{position:absolute;left:6px;right:6px;bottom:6px;line-height:1.5}
  #now:empty{display:none}
  #aviso{text-align:center;color:#9ca3af;padding:20px}
  body.so-pip header h1,body.so-pip .lista{display:none}
  body.so-pip main{grid-template-columns:1fr;padding:8px}
</style></head><body>
<header><h1>📷 Monitor da Câmera</h1>
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
      <button class="seta esq" data-d="esquerda" title="girar para a esquerda">◀</button>
      <button class="seta dir" data-d="direita" title="girar para a direita">▶</button>
      <div id="now"></div>
    </div>
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
// PTZ: botões da página e, no PiP, os controles de mídia ⏮ ⏭ (lá não há DOM)
let ptzOcupado=false;
async function girar(d){
  if(ptzOcupado) return;
  ptzOcupado=true; painel.querySelectorAll('.seta').forEach(b=>b.disabled=true);
  try{ await fetch('/ptz/'+d,{method:'POST'}); }catch(e){}
  // a câmera engole comando que chega com o motor andando: um passo leva ~3s
  setTimeout(()=>{ ptzOcupado=false; painel.querySelectorAll('.seta').forEach(b=>b.disabled=false); },3000);
}
painel.querySelectorAll('.seta').forEach(b=>b.onclick=()=>girar(b.dataset.d));
if('mediaSession' in navigator){
  navigator.mediaSession.metadata=new MediaMetadata({title:'Monitor da Câmera',artist:'⏮ ⏭ giram a câmera'});
  navigator.mediaSession.setActionHandler('previoustrack',()=>girar('esquerda'));
  navigator.mediaSession.setActionHandler('nexttrack',()=>girar('direita'));
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
    assert ALAW[0x55] == -8 and ALAW[0xD5] == 8 and ALAW[0x2A] == -32256 and ALAW[0xAA] == 32256


if __name__ == "__main__":
    _selfcheck()
    os.makedirs(FOTOS_DIR, exist_ok=True)
    threading.Thread(target=audio_loop, daemon=True).start()
    audio_ready.wait(8)  # áudio conecta antes: a câmera só aceita 1 sessão de áudio
    threading.Thread(target=capture_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)
