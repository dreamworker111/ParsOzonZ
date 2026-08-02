"""Test CDP: desktop warmup then mobile navigation."""

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    connect_via_cdp,
    ensure_chrome_for_ozon,
    is_blocked_page,
    wait_for_ozon_ready,
    _apply_mobile_cdp_emulation,
)
from ozon_parser.config import DESKTOP_BASE_URL, MOBILE_WARMUP_URL


def main() -> None:
    with sync_playwright() as pw:
        ensure_chrome_for_ozon(print)
        browser, context, page = connect_via_cdp(pw, print)
        page.goto(DESKTOP_BASE_URL + "/", wait_until="domcontentloaded", timeout=90000)
        print("DESKTOP READY", wait_for_ozon_ready(page, print, timeout_sec=120))
        print("DESKTOP BLOCKED", is_blocked_page(page))
        print("DESKTOP", page.url, page.title())

        _apply_mobile_cdp_emulation(page)
        page.goto(MOBILE_WARMUP_URL, wait_until="domcontentloaded", timeout=90000)
        print("MOBILE READY", wait_for_ozon_ready(page, print, timeout_sec=120))
        print("MOBILE BLOCKED", is_blocked_page(page))
        print("MOBILE", page.url, page.title())

        page.goto("https://m.ozon.ru/seller/", wait_until="domcontentloaded", timeout=90000)
        print("SELLER READY", wait_for_ozon_ready(page, print, timeout_sec=120))
        print("SELLER BLOCKED", is_blocked_page(page))
        print("SELLER", page.url, page.title())


if __name__ == "__main__":
    main()
