@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else (
    set PYTHON=python
)

"%PYTHON%" -m pip install -r requirements.txt pyinstaller -q
"%PYTHON%" -m playwright install chromium

"%PYTHON%" -m PyInstaller --noconfirm --onefile --windowed ^
    --name "OzonParser" ^
    --add-data "assets;assets" ^
    --hidden-import=playwright ^
    --hidden-import=openpyxl ^
    --hidden-import=browser_cookie3 ^
    main.py

echo.
echo Сборка завершена: dist\OzonParser.exe
echo Для первого запуска установите браузер: playwright install chromium
pause
