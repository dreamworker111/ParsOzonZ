"""Test CDP Chrome-for-Ozon tab through antibot wait."""

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    ensure_chrome_for_ozon,
    is_antibot_challenge_page,
    is_blocked_page,
    wait_for_ozon_ready,
    connect_via_cdp,
)
from ozon_parser.config import MOBILE_WARMUP_URL


def main() -> None:
    with sync_playwright() as pw:
        ensure_chrome_for_ozon(print)
        browser, context, page = connect_via_cdp(pw, print)
        print("START", page.url, page.title())
        page.goto(MOBILE_WARMUP_URL, wait_until="domcontentloaded", timeout=90000)
        print("READY", wait_for_ozon_ready(page, print, timeout_sec=120))
        print("ANTIBOT", is_antibot_challenge_page(page))
        print("BLOCKED", is_blocked_page(page))
        print("URL", page.url)
        print("TITLE", page.title())


if __name__ == "__main__":
    main()
