import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    ensure_chrome_for_ozon,
    is_blocked_page,
    is_antibot_challenge_page,
    wait_for_ozon_ready,
    connect_via_cdp,
    _apply_mobile_cdp_emulation,
)
from ozon_parser.config import DESKTOP_BASE_URL
from ozon_parser.chrome_launcher import is_cdp_available

print("CDP before", is_cdp_available())
print("ensure", ensure_chrome_for_ozon(print))
print("CDP after", is_cdp_available())

with sync_playwright() as pw:
    browser, context, page = connect_via_cdp(pw, print)
    print("start", page.url, page.title())
    _apply_mobile_cdp_emulation(page)
    page.goto(DESKTOP_BASE_URL + "/", wait_until="domcontentloaded", timeout=90000)
    time.sleep(2)
    print("after goto", page.url, page.title())
    print("antibot", is_antibot_challenge_page(page))
    print("blocked", is_blocked_page(page))
    print("ready", wait_for_ozon_ready(page, print, timeout_sec=120))
    print("final blocked", is_blocked_page(page))
    print("final title", page.title())
