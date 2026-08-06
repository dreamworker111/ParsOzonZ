"""Live CDP navigation smoke test for Ozon root URLs."""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    connect_via_cdp,
    ensure_ozon_session_ready,
    is_blocked_page,
    page_has_usable_ozon_content,
    safe_goto,
)
from ozon_parser.config import ALL_SELLERS_PATH, DESKTOP_BASE_URL, GLOBAL_CATALOG_PATH


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    base = DESKTOP_BASE_URL.rstrip("/")
    targets = [
        base + ALL_SELLERS_PATH,
        base + "/",
        base + GLOBAL_CATALOG_PATH,
    ]
    with sync_playwright() as pw:
        _browser, _context, page = connect_via_cdp(pw, print)
        print("connected", page.url)
        if not ensure_ozon_session_ready(page, print):
            print("FAIL session warmup")
            return 1
        print("warmup_ok", page.url)
        ok_any = False
        for url in targets:
            ok = safe_goto(page, url, print)
            usable = page_has_usable_ozon_content(page)
            blocked = is_blocked_page(page)
            print(f"goto {url} -> ok={ok} usable={usable} blocked={blocked} current={page.url}")
            ok_any = ok_any or (ok and usable and not blocked)
        print("PASS" if ok_any else "FAIL")
        return 0 if ok_any else 1


if __name__ == "__main__":
    raise SystemExit(main())
