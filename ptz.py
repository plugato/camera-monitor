#!/usr/bin/env python3
"""Move a câmera (pan). Uso: ./ptz.py {esquerda|direita} [passos]

A câmera é uma Yoosee/Gwell e o caminho de controle não é óbvio:

  - A porta 5000 é um servidor ONVIF gSOAP, mas só responde no endpoint certo:
    um POST em /onvif/device_service ou /onvif/ptz_service é aceito com
    200 OK e ContinuousMoveResponse -- e não move nada. O endpoint real vem do
    GetCapabilities, no campo <tt:PTZ><tt:XAddr>: /onvif/deviceio_service.
  - O firmware ignora a duração do movimento: cada ContinuousMove anda um passo
    só, e o passo é irregular -- medi 41 a 200px, com a direita andando mais que
    a esquerda. Não dá controle fino nem ida-e-volta simétrica.
  - Tilt (y): a nota antiga dizia que não move; a medição de 2026-10 foi
    inconclusiva. O painel manda e, se não mover, nada quebra.
  - Cada ContinuousMove anda um passo de ~0,3s; a chamada SOAP leva ~0,7s.
    Comandos em sequência, um após a resposta do outro, andam um passo cada
    (medido em 2026-10: 6 seguidos ~ 6 passos). O Stop não para nada e só
    dobrava o tempo de cada passo: saiu.
  - RTSP também mente: OPTIONS anuncia USER_CMD_SET e SET_PARAMETER responde
    200 OK a qualquer ptzCmd, sem mover. Não use.
"""
import socket
import sys

import config

HOST = config.CAM_HOST
PORT = config.CAM_ONVIF_PORT
ENDPOINT = "/onvif/deviceio_service"
TOKEN = "IPCProfilesToken0"          # de GetProfiles no /onvif/media_service
VELOCIDADE = {
  "esquerda": (-1, 0),
  "direita": (1, 0),
  "cima": (0, 1),
  "baixo": (0, -1),
}

ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
 xmlns:ptz="http://www.onvif.org/ver20/ptz/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema">
<soap:Body>{corpo}</soap:Body></soap:Envelope>"""


def soap(corpo):
    xml = ENVELOPE.format(corpo=corpo)
    req = (f"POST {ENDPOINT} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Content-Type: application/soap+xml; charset=utf-8\r\n"
           f"Content-Length: {len(xml)}\r\nConnection: close\r\n\r\n{xml}")
    with socket.create_connection((HOST, PORT), 8) as s:
        s.settimeout(8)
        s.sendall(req.encode())
        resp = b""
        while True:
            c = s.recv(8192)
            if not c:
                return resp.decode(errors="replace")
            resp += c


def mover(direcao, passos=1):
    x, y = VELOCIDADE[direcao]
    for _ in range(passos):    # em sequência: o próximo sai após a resposta
        soap(f'<ptz:ContinuousMove><ptz:ProfileToken>{TOKEN}</ptz:ProfileToken>'
             f'<ptz:Velocity><tt:PanTilt x="{x}" y="{y}"/></ptz:Velocity>'
             f'</ptz:ContinuousMove>')


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in VELOCIDADE:
        sys.exit(__doc__)
    mover(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    print(f"{sys.argv[1]}: ok")
