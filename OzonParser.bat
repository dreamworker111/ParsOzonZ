@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "dist\OzonParser.exe" (
    start "" "%~dp0dist\OzonParser.exe"
    exit /b 0
)

echo Приложение ещё не собрано.
echo Сейчас будет создан файл dist\OzonParser.exe ...
echo.
call "%~dp0build.bat"
if exist "dist\OzonParser.exe" (
    start "" "%~dp0dist\OzonParser.exe"
) else (
    echo Не удалось собрать приложение.
    pause
)
