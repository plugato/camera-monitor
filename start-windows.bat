@echo off
cd /d "%~dp0"
taskkill /F /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq Camera Monitor" >nul 2>&1
start "Camera Monitor" /b py server.py > server.log 2>&1
echo Monitor no ar: http://localhost:8090