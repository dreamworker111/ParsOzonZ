"""Compare desktop vs mobile Ozon access for diagnostics."""

from playwright.sync_api import sync_playwright

from ozon_parser.browser import is_blocked_page, extract_incident_id
from ozon_parser.config import DESKTOP_BASE_URL, MOBILE_WARMUP_URL


def check(url: str, page) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    blocked = is_blocked_page(page)
    print(f"URL: {url}")
    print(f"BLOCKED: {blocked}")
    if blocked:
        print(f"INCIDENT: {extract_incident_id(page)}")
    else:
        snippet = page.evaluate(
            "() => (document.title || '') + ' | ' + (document.body?.innerText || '').slice(0,120)"
        )
        print(f"OK: {snippet.replace(chr(10), ' ')}")


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context(
            locale="ru-RU",
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()
        check(DESKTOP_BASE_URL + "/seller/", page)
        print("---")
        check(MOBILE_WARMUP_URL, page)
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
