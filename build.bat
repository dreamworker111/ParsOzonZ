@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Сборка OzonParser.exe

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

echo Установка зависимостей для сборки...
"%PYTHON%" -m pip install -r requirements.txt pyinstaller -q
if errorlevel 1 (
    echo Не удалось установить зависимости.
    pause
    exit /b 1
)

echo Сборка однофайлового приложения...
"%PYTHON%" -m PyInstaller --noconfirm --clean OzonParser.spec
if errorlevel 1 (
    echo Сборка не удалась.
    pause
    exit /b 1
)

if not exist "dist\OzonParser.exe" (
    echo Файл dist\OzonParser.exe не найден.
    pause
    exit /b 1
)

echo.
echo Готово: dist\OzonParser.exe
echo Этот файл можно копировать на любой Windows ПК с установленным Google Chrome или Edge.
echo Двойной клик по OzonParser.exe запускает приложение без Python и IDE.
echo.
pause
