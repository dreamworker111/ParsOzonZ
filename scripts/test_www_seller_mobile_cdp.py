"""Test www seller page via CDP for mobile guest."""

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    connect_via_cdp,
    ensure_chrome_for_ozon,
    is_blocked_page,
    wait_for_ozon_ready,
    _apply_mobile_cdp_emulation,
)
from ozon_parser.config import DESKTOP_BASE_URL


def main() -> None:
    with sync_playwright() as pw:
        ensure_chrome_for_ozon(print)
        browser, context, page = connect_via_cdp(pw, print)
        page.goto(DESKTOP_BASE_URL + "/seller/", wait_until="domcontentloaded", timeout=90000)
        _apply_mobile_cdp_emulation(page)
        print("READY", wait_for_ozon_ready(page, print, timeout_sec=120))
        print("BLOCKED", is_blocked_page(page))
        print("URL", page.url)
        print("TITLE", page.title())
        text = page.evaluate("() => (document.body?.innerText || '').slice(0, 300)")
        print("TEXT", text.replace("\n", " ")[:300])


if __name__ == "__main__":
    main()
