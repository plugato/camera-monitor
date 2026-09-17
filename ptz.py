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
  - Tilt (y) não faz nada nesta câmera: ela só gira na horizontal.
  - Comando que chega com o motor andando é ENGOLIDO (medido: dois comandos a
    0,5s de intervalo movem o mesmo que um). Espere ~3s entre os passos --
    é o que o `passos` daqui já faz, com pausa de 3s.
  - RTSP também mente: OPTIONS anuncia USER_CMD_SET e SET_PARAMETER responde
    200 OK a qualquer ptzCmd, sem mover. Não use.
"""
import socket
import sys
import time

import config

HOST = config.CAM_HOST
PORT = config.CAM_ONVIF_PORT
ENDPOINT = "/onvif/deviceio_service"
TOKEN = "IPCProfilesToken0"          # de GetProfiles no /onvif/media_service
VELOCIDADE = {"esquerda": -1, "direita": 1}

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


def mover(direcao, passos=1, pausa=3.0):
    x = VELOCIDADE[direcao]
    for i in range(passos):
        if i:
            time.sleep(pausa)      # sem isso o passo seguinte é ignorado
        soap(f'<ptz:ContinuousMove><ptz:ProfileToken>{TOKEN}</ptz:ProfileToken>'
             f'<ptz:Velocity><tt:PanTilt x="{x}" y="0"/></ptz:Velocity>'
             f'</ptz:ContinuousMove>')
        # o Stop não interrompe o passo (ele é fixo), mas evita deixar a
        # câmera em movimento se um firmware futuro respeitar a duração
        soap(f'<ptz:Stop><ptz:ProfileToken>{TOKEN}</ptz:ProfileToken>'
             f'<ptz:PanTilt>true</ptz:PanTilt></ptz:Stop>')


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in VELOCIDADE:
        sys.exit(__doc__)
    mover(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    print(f"{sys.argv[1]}: ok")
