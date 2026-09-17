"""Regressão da detecção. Rodar: .venv/bin/python test_detect.py

Dois lados:
  - as fotos de animal anteriores ao CORTE são os falsos positivos conhecidos
    (carro e grade do portão que o modelo chamava de pássaro/gato): nenhuma pode
    detectar nada hoje;
  - as fotos de pessoa são detecções reais: todas têm que continuar detectando.

Foto de animal posterior ao CORTE não entra: não sabemos se é bicho de verdade.
"""
import glob
import pathlib

import cv2

import server

CORTE = "20260917_120000"        # a correção do filtro de área entrou às 11:05

falsos = [f for f in glob.glob("fotos/*_animal.jpg") if pathlib.Path(f).name < CORTE]
pessoas = glob.glob("fotos/*_pessoa.jpg")
assert falsos and pessoas, "faltam fotos de referência em fotos/"

for f in falsos:
    dets = server.detect(cv2.imread(f))
    assert not dets, f"{f}: voltou o falso positivo {dets}"

for f in pessoas:
    dets = server.detect(cv2.imread(f))
    assert any(d["category"] == "pessoa" for d in dets), f"{f}: deixou de ver a pessoa"

print(f"ok: {len(falsos)} falsos positivos barrados, {len(pessoas)} pessoas detectadas")
