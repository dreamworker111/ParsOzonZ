"""Compare /category/ vs /seller/?category= navigation on live Ozon."""

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    connect_via_cdp,
    ensure_chrome_for_ozon,
    is_access_restricted,
    wait_for_ozon_ready,
)
from ozon_parser.config import DESKTOP_BASE_URL

CATEGORY_ID = "15500"


def check(page, label: str) -> bool:
    ready = wait_for_ozon_ready(page, print, timeout_sec=90)
    restricted = is_access_restricted(page)
    title = page.title()
    print(f"{label}: ready={ready} restricted={restricted} title={title!r} url={page.url}")
    return ready and not restricted and "нет соединения" not in title.lower()


def main() -> int:
    ensure_chrome_for_ozon(print)
    with sync_playwright() as pw:
        browser, context, page = connect_via_cdp(pw, print)
        page.goto(DESKTOP_BASE_URL + "/seller/", wait_until="domcontentloaded", timeout=90000)
        if not check(page, "SELLER WARMUP"):
            return 1

        page.goto(
            f"{DESKTOP_BASE_URL}/category/{CATEGORY_ID}/",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        category_ok = check(page, "DIRECT /category/")
        time.sleep(3)

        page.goto(DESKTOP_BASE_URL + "/seller/", wait_until="domcontentloaded", timeout=90000)
        wait_for_ozon_ready(page, print, timeout_sec=60)
        page.goto(
            f"{DESKTOP_BASE_URL}/seller/?category={CATEGORY_ID}&sorting=price",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        seller_ok = check(page, "SELLER ?category=")

        print("RESULT category_path=", category_ok, "seller_filter=", seller_ok)
        return 0 if seller_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
