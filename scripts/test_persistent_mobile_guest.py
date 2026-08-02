"""Test persistent mobile guest profile with antibot wait."""

from playwright.sync_api import sync_playwright

from ozon_parser.auth import mobile_guest_profile_dir
from ozon_parser.browser import (
    create_persistent_context,
    is_antibot_challenge_page,
    is_blocked_page,
    prepare_mobile_guest_session,
    wait_for_ozon_ready,
)
from ozon_parser.config import MOBILE_MODE, MOBILE_WARMUP_URL


def main() -> None:
    with sync_playwright() as pw:
        context, page = create_persistent_context(
            pw,
            headless=False,
            user_data_dir=mobile_guest_profile_dir(),
            browser_mode=MOBILE_MODE,
        )
        print("PROFILE", mobile_guest_profile_dir())
        ok = prepare_mobile_guest_session(page, print)
        print("PREPARE", ok)
        print("ANTIBOT", is_antibot_challenge_page(page))
        print("BLOCKED", is_blocked_page(page))
        print("URL", page.url)
        print("TITLE", page.title())
        if ok:
            page.goto("https://m.ozon.ru/seller/", wait_until="domcontentloaded", timeout=90000)
            print("READY", wait_for_ozon_ready(page, print, timeout_sec=120))
            print("SELLER BLOCKED", is_blocked_page(page))
            print("SELLER TITLE", page.title())
        context.close()


if __name__ == "__main__":
    main()
