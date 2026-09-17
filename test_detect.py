"""Regressão: nenhuma das fotos salvas tem bicho — todas são carro/grade que o
modelo chamou de pássaro/gato. Rodar: .venv/bin/python test_detect.py"""
import glob
import cv2
import server

for f in sorted(glob.glob("fotos/*.jpg")):
    dets = server.detect(cv2.imread(f))
    assert not dets, f"{f}: falso positivo {dets}"
print(f"ok: {len(glob.glob('fotos/*.jpg'))} fotos, zero detecção")
