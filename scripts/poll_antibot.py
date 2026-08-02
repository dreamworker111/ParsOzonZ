"""Poll antibot challenge until it resolves or times out."""

import time

from playwright.sync_api import sync_playwright

from ozon_parser.browser import is_blocked_page, is_captcha_page
from ozon_parser.config import MOBILE_USER_AGENT, MOBILE_VIEWPORT, MOBILE_WARMUP_URL


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context(
            locale="ru-RU",
            user_agent=MOBILE_USER_AGENT,
            viewport=MOBILE_VIEWPORT,
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()
        page.goto(MOBILE_WARMUP_URL, wait_until="domcontentloaded", timeout=90000)

        for step in range(18):
            title = page.title()
            url = page.url
            text = page.evaluate("() => (document.body?.innerText || '').slice(0, 200)")
            print(
                step,
                title,
                url,
                repr(text[:80]),
                "blocked=",
                is_blocked_page(page),
                "captcha=",
                is_captcha_page(page),
            )
            if "antibot challenge page" not in title.lower():
                break
            time.sleep(5)

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
