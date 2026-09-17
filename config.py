"""Endereço e credenciais da câmera, lidos do .env (que não vai para o git).

Sem dependência nova: o .env aqui é só `CHAVE=valor` por linha. Quem não achar
a variável morre na subida com mensagem clara, em vez de falhar depois com
401 no meio do stream.
"""
import os
import pathlib


def _carrega_env(nome=".env"):
    arq = pathlib.Path(__file__).with_name(nome)
    if not arq.exists():
        return
    for linha in arq.read_text().splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        # variável já exportada no ambiente tem precedência sobre o arquivo
        os.environ.setdefault(chave.strip(), valor.strip().strip("\"'"))


def _obrigatoria(nome):
    valor = os.environ.get(nome)
    if not valor:
        raise SystemExit(f"falta {nome}: copie o .env.exemplo para .env e preencha")
    return valor


_carrega_env()

CAM_HOST = _obrigatoria("CAM_HOST")
CAM_USER = _obrigatoria("CAM_USER")
CAM_PASS = _obrigatoria("CAM_PASS")
CAM_RTSP_PORT = int(os.environ.get("CAM_RTSP_PORT", "554"))
CAM_ONVIF_PORT = int(os.environ.get("CAM_ONVIF_PORT", "5000"))
CAM_PATH = os.environ.get("CAM_PATH", "onvif1")

RTSP_URL = f"rtsp://{CAM_USER}:{CAM_PASS}@{CAM_HOST}:{CAM_RTSP_PORT}/{CAM_PATH}"
