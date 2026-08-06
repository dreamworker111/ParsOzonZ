@echo off
chcp 65001 >nul
title Chrome для Ozon Parser

set PROFILE=C:\Ozon\ChromeProfile
set PORT=9222

if not exist "%PROFILE%" mkdir "%PROFILE%"

set CHROME=
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe

if "%CHROME%"=="" (
    echo Google Chrome не найден. Установите Chrome и повторите.
    pause
    exit /b 1
)

echo Запуск Chrome для парсера Ozon...
echo Профиль: %PROFILE%
echo Порт отладки: %PORT%
echo.
echo Не закрывайте это окно пока работает парсер.
echo.

start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check about:blank

echo Chrome запущен. Теперь запустите парсер.
pause
