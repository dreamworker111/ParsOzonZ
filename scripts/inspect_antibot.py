"""Inspect Ozon antibot challenge page content."""

from playwright.sync_api import sync_playwright

from ozon_parser.config import MOBILE_WARMUP_URL


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context(locale="ru-RU")
        page = context.new_page()
        page.goto(MOBILE_WARMUP_URL, wait_until="domcontentloaded", timeout=90000)
        data = page.evaluate(
            """() => ({
                title: document.title,
                url: location.href,
                html: document.documentElement.outerHTML.slice(0, 4000),
                text: document.body?.innerText || '',
            })"""
        )
        print("TITLE:", data["title"])
        print("URL:", data["url"])
        print("TEXT:", repr(data["text"][:500]))
        print("HTML:", data["html"][:2000])
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
