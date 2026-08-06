import os
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import CDP_URL, CHROME_DEBUG_PORT, CHROME_OZON_PROFILE, DESKTOP_BASE_URL

OZON_START_URL = DESKTOP_BASE_URL.rstrip("/") + "/"
CDP_PROCESS_MARKER = f"--remote-debugging-port={CHROME_DEBUG_PORT}"


def find_chrome_exe() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def is_cdp_available() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_cdp(timeout_sec: int = 30) -> bool:
    for _ in range(timeout_sec):
        if is_cdp_available():
            return True
        time.sleep(1)
    return False


def launch_chrome_for_ozon() -> bool:
    chrome = find_chrome_exe()
    if not chrome:
        return False

    CHROME_OZON_PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(chrome),
            f"--remote-debugging-port={CHROME_DEBUG_PORT}",
            f"--user-data-dir={CHROME_OZON_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return True


def kill_ozon_chrome_processes() -> int:
    """Stop Chrome instances launched for the Ozon parser (debug port 9222)."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{CDP_PROCESS_MARKER}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; 1 } | "
        "Measure-Object -Sum | Select-Object -ExpandProperty Sum"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    except Exception:
        pass
    return 0


def restart_chrome_for_ozon(progress=None) -> bool:
    """Restart the dedicated Chrome-for-Ozon process."""
    log = progress or (lambda _m: None)
    log("Перезапуск Chrome для Ozon...")
    kill_ozon_chrome_processes()
    time.sleep(2)
    if not launch_chrome_for_ozon():
        return False
    if wait_for_cdp(30):
        time.sleep(2.5)
        log("Chrome перезапущен")
        return True
    return False


def ensure_chrome_for_ozon(progress=None) -> bool:
    log = progress or (lambda _m: None)
    if is_cdp_available():
        return True
    log("Запуск Chrome для Ozon...")
    if not launch_chrome_for_ozon():
        return False
    if wait_for_cdp(30):
        # Chrome may expose CDP before the network stack is ready for navigation.
        time.sleep(2.5)
        log("Chrome готов")
        return True
    return False
