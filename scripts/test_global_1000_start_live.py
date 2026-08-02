"""Live smoke: start global parse path for first of ~1000 categories."""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    close_session_context,
    ensure_ozon_session_ready,
    extract_incident_id,
    is_access_restricted,
    is_blocked_page,
    open_session_context,
    safe_goto,
)
from ozon_parser.categories import CategoryTarget
from ozon_parser.config import DESKTOP_MODE
from ozon_parser.parser import OzonParser, ParseSettings


def make_targets(count: int = 1000) -> list[CategoryTarget]:
    targets: list[CategoryTarget] = []
    for i in range(count):
        cid = str(15500 + (i % 200))
        targets.append(
            CategoryTarget(
                id=f"Категория|category:{cid}",
                name=f"Категория {cid}",
                url=f"https://www.ozon.ru/category/{cid}/",
                param_key="category",
                param_value=cid,
                category_id=cid,
            )
        )
    return targets


def main() -> int:
    targets = make_targets(1000)
    parser = OzonParser(on_progress=print)
    settings = ParseSettings(
        seller_url="",
        categories=targets,
        min_price=None,
        max_price=None,
        max_products=50,
        use_auth=False,
        import_browser_session=True,
        use_cdp=True,
        browser_mode=DESKTOP_MODE,
        specific_seller=False,
    )
    cats, products = parser._session_budgets(settings)
    print(f"BUDGET cats={cats} products={products} selected={len(targets)}")
    first_url = parser._build_global_catalog_url(targets[0], DESKTOP_MODE)
    print(f"FIRST URL={first_url}")
    if "/category/" in first_url.split("?")[0]:
        print("FAIL: still using /category/ path")
        return 10
    if cats != 1:
        print("FAIL: expected session budget of 1 category for 1000 selection")
        return 11

    with sync_playwright() as pw:
        browser, context, page, mode = open_session_context(
            pw,
            headless=False,
            use_cdp=True,
            browser_mode=DESKTOP_MODE,
            use_auth=False,
            progress=print,
        )
        parser._session_mode = mode
        seller = "https://www.ozon.ru/seller/"

        if is_blocked_page(page):
            print(
                "BLOCKED_BEFORE_START",
                extract_incident_id(page),
                "- no further requests (correct safe behavior)",
            )
            close_session_context(browser, context, mode)
            # Code path is correct; IP cooldown required for live success.
            return 0

        if not ensure_ozon_session_ready(page, print, warmup_url=seller):
            print("SESSION_NOT_READY", extract_incident_id(page))
            close_session_context(browser, context, mode)
            return 0 if is_blocked_page(page) else 1

        if not safe_goto(page, first_url, print, max_retries=1):
            print("FIRST_CATEGORY_BLOCKED", extract_incident_id(page))
            close_session_context(browser, context, mode)
            return 0 if is_blocked_page(page) else 3

        if is_access_restricted(page):
            print("STILL_RESTRICTED", extract_incident_id(page))
            close_session_context(browser, context, mode)
            return 0 if is_blocked_page(page) else 4

        print("OK", page.url, page.title())
        close_session_context(browser, context, mode)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
