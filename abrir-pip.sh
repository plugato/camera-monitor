#!/usr/bin/env bash
# Abre o painel flutuante do monitor.
#
# Sobe uma janela mínima do Chrome (sem abas nem barra de endereço) que solta o
# painel de PiP no primeiro clique. Ao minimizar essa janela ela some da barra de
# tarefas e fica só no tray, ao lado do relógio (clique no ícone para trazê-la).
#
# A janela pequena não pode ser FECHADA: o Chrome amarra a janela de PiP à página
# que a abriu e fecha as duas juntas. Minimizar (= tray) é o caminho.
cd "$(dirname "$0")"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w tray.ps1)" >/dev/null 2>&1 &
echo "Clique uma vez na janela que abriu para soltar o painel flutuante."
echo "Depois minimize-a: ela vai para o tray e o painel continua por cima de tudo."
