@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else (
    set PYTHON=python
)

if not exist "assets\fonts\Roboto-Light.ttf" (
    echo Загрузка шрифта Roboto Light...
    powershell -Command "New-Item -ItemType Directory -Force -Path 'assets\fonts' | Out-Null; Invoke-WebRequest -Uri 'https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Light.ttf' -OutFile 'assets\fonts\Roboto-Light.ttf'"
)

if not exist "C:\Ozon" mkdir "C:\Ozon"

"%PYTHON%" -m pip install -r requirements.txt -q
"%PYTHON%" -m playwright install chromium

"%PYTHON%" main.py
pause
