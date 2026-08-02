"""Live test: global catalog parse navigation via seller filter URLs."""

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    close_session_context,
    ensure_ozon_session_ready,
    is_access_restricted,
    is_blocked_page,
    open_session_context,
    safe_goto,
)
from ozon_parser.categories import CategoryTarget
from ozon_parser.config import DESKTOP_MODE
from ozon_parser.parser import OzonParser


def main() -> int:
    parser = OzonParser(on_progress=print)
    target = CategoryTarget(
        id="Категория|category:15500",
        name="Электроника",
        url="https://www.ozon.ru/category/15500/",
        category_id="15500",
        param_key="category",
        param_value="15500",
    )
    catalog_url = parser._build_global_catalog_url(target, DESKTOP_MODE)
    print("CATALOG URL:", catalog_url)

    with sync_playwright() as pw:
        browser, context, page, mode = open_session_context(
            pw,
            headless=False,
            use_cdp=True,
            browser_mode=DESKTOP_MODE,
            use_auth=False,
        )
        parser._session_mode = mode
        seller_url = "https://www.ozon.ru/seller/"

        if not ensure_ozon_session_ready(page, print, warmup_url=seller_url):
            print("SESSION NOT READY")
            close_session_context(browser, context, mode)
            return 1

        print("SESSION OK:", page.url, page.title())
        ok = safe_goto(page, catalog_url, print)
        print("NAV OK:", ok)
        print("BLOCKED:", is_blocked_page(page))
        print("RESTRICTED:", is_access_restricted(page))
        print("URL:", page.url)
        print("TITLE:", page.title())
        text = page.evaluate("() => (document.body?.innerText || '').slice(0, 200)")
        print("TEXT:", text.replace("\n", " ")[:200])

        close_session_context(browser, context, mode)
        if not ok or is_access_restricted(page):
            return 2
        if "нет соединения" in (page.title() or "").lower():
            return 3
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
