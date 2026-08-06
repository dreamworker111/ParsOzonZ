"""Diagnose CDP Chrome navigation to ozon.ru after a clean restart."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = Path(r"C:\Ozon\ChromeProfile")
PORT = 9222
CDP = f"http://127.0.0.1:{PORT}"


def cdp_up() -> bool:
    try:
        urllib.request.urlopen(f"{CDP}/json/version", timeout=2)
        return True
    except Exception:
        return False


def find_chrome() -> Path | None:
    for p in (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ):
        if p.exists():
            return p
    return None


def restart_chrome() -> None:
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    time.sleep(2)
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("Chrome not found")
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(chrome),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for i in range(30):
        if cdp_up():
            print(f"cdp ready after {i}s")
            return
        time.sleep(1)
    raise RuntimeError("CDP did not start")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("Restarting Chrome...")
    restart_chrome()
    time.sleep(3)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        print("existing pages:", [p.url for p in ctx.pages])
        page = ctx.new_page()
        print("new page:", page.url)

        for wait in ("commit", "domcontentloaded", "load"):
            try:
                page.goto("https://www.ozon.ru/", wait_until=wait, timeout=45000)
                print(f"OK goto wait={wait!r} url={page.url} title={page.title()[:80]!r}")
                return 0
            except Exception as exc:
                print(f"FAIL goto wait={wait!r}: {exc}")

        try:
            page.goto("about:blank", wait_until="commit", timeout=10000)
            page.evaluate("() => { window.location.href = 'https://www.ozon.ru/'; }")
            page.wait_for_load_state("domcontentloaded", timeout=45000)
            print(f"OK js-nav url={page.url} title={page.title()[:80]!r}")
            return 0
        except Exception as exc:
            print(f"FAIL js-nav: {exc}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
