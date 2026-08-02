"""End-to-end mobile guest session test."""

import sys

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    close_session_context,
    is_blocked_page,
    open_session_context,
    wait_for_ozon_ready,
)
from ozon_parser.categories import CategoryLoader
from ozon_parser.config import MOBILE_MODE


def main() -> int:
    with sync_playwright() as pw:
        try:
            browser, context, page, mode = open_session_context(
                pw,
                headless=False,
                browser_mode=MOBILE_MODE,
                use_auth=False,
            )
        except Exception as exc:
            print(f"OPEN FAILED: {exc}")
            return 1

        print(f"MODE={mode}")
        print(f"URL={page.url}")
        print(f"TITLE={page.title()}")
        print(f"READY={wait_for_ozon_ready(page, print, timeout_sec=30)}")
        print(f"BLOCKED={is_blocked_page(page)}")

        loader = CategoryLoader(page, MOBILE_MODE, session_mode=mode)
        roots = loader._load_global_root_categories(print, None)
        print(f"ROOTS={len(roots)}")
        if roots:
            print("FIRST", roots[0].get("name"), roots[0].get("id"))

        close_session_context(browser, context, mode)
        return 0 if len(roots) >= 2 else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
