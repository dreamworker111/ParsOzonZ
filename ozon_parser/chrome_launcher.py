import os
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import CDP_URL, CHROME_DEBUG_PORT, CHROME_OZON_PROFILE, DESKTOP_BASE_URL

OZON_START_URL = DESKTOP_BASE_URL.rstrip("/") + "/"


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
            OZON_START_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return True


def ensure_chrome_for_ozon(progress=None) -> bool:
    log = progress or (lambda _m: None)
    if is_cdp_available():
        return True
    log("Запуск Chrome для Ozon...")
    if not launch_chrome_for_ozon():
        return False
    if wait_for_cdp(30):
        log("Chrome готов")
        return True
    return False
