# Abre o monitor em janela de app e, ao minimizar, some da barra de tarefas e
# fica só no tray (relógio). Clique no ícone para trazer de volta.
#
# O Windows não tem "minimizar para o tray" nativo: o que se faz é esconder a
# janela (SW_HIDE, que tira também o botão da barra) e desenhar um NotifyIcon
# próprio para trazê-la de volta.
param([string]$Url = "http://localhost:8090/?pip=1")

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Janela {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
"@

$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) {
  $chrome = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
}
# --user-data-dir separado: garante processo e janela próprios, que dá para achar
$perfil = Join-Path $env:LOCALAPPDATA "monitor-camera-perfil"
$proc = Start-Process $chrome -PassThru -ArgumentList `
  "--app=$Url", "--window-size=520,320", "--user-data-dir=`"$perfil`""

# a janela demora a existir; espera até uns 15s por ela
# $script: em tudo que os blocos de evento usam: handler de evento no PowerShell
# roda em outro escopo e não enxerga variável local da raiz do script
$script:hwnd = [IntPtr]::Zero
$script:proc = $proc
for ($i = 0; $i -lt 60 -and $script:hwnd -eq [IntPtr]::Zero; $i++) {
  Start-Sleep -Milliseconds 250
  $script:proc.Refresh()
  if ($script:proc.MainWindowHandle -ne [IntPtr]::Zero) { $script:hwnd = $script:proc.MainWindowHandle }
  else {
    $j = Get-Process -Name chrome, msedge -ErrorAction SilentlyContinue |
         Where-Object { $_.MainWindowTitle -like "*Monitor da C*" } | Select-Object -First 1
    if ($j) { $script:hwnd = $j.MainWindowHandle; $script:proc = $j }
  }
}
if ($script:hwnd -eq [IntPtr]::Zero) { Write-Error "não achei a janela do monitor"; exit 1 }

$script:icone = New-Object System.Windows.Forms.NotifyIcon
$icone = $script:icone
$icone.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($chrome)
$icone.Text = "Monitor da Câmera"
$icone.Visible = $true

$script:mostrar = {
  [Janela]::ShowWindow($script:hwnd, 9) | Out-Null    # 9 = SW_RESTORE
  [Janela]::SetForegroundWindow($script:hwnd) | Out-Null
}
$icone.add_Click($script:mostrar)

$menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$menu.Items.Add("Mostrar", $null, [System.EventHandler]{ & $script:mostrar })
[void]$menu.Items.Add("Sair", $null, [System.EventHandler]{
  $script:icone.Visible = $false
  Stop-Process -Id $script:proc.Id -Force -ErrorAction SilentlyContinue
  [System.Windows.Forms.Application]::Exit()
})
$icone.ContextMenuStrip = $menu

# vigia: minimizou -> esconde (some da barra de tarefas). O PiP é outra janela e
# continua flutuando normalmente, porque a página dona segue viva, só invisível.
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 400
$timer.add_Tick({
  if ($script:proc.HasExited) { $script:icone.Visible = $false; [System.Windows.Forms.Application]::Exit(); return }
  if ([Janela]::IsIconic($script:hwnd)) { [Janela]::ShowWindow($script:hwnd, 0) | Out-Null }   # 0 = SW_HIDE
})
$timer.Start()

[System.Windows.Forms.Application]::Run()
$icone.Dispose()
