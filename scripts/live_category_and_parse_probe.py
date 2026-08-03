"""Live probe: load root categories and soft-start one seller-filter URL.

Loops until success. If fab_/block is active, waits passively (no reload).
"""

from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    close_session_context,
    ensure_ozon_session_ready,
    extract_incident_id,
    is_access_restricted,
    is_antibot_challenge_page,
    is_blocked_page,
    open_session_context,
    page_has_usable_ozon_content,
    safe_goto,
    wait_for_ozon_ready,
)
from ozon_parser.categories import CategoryLoader
from ozon_parser.config import DESKTOP_MODE

MAX_ROUNDS = 12
BLOCK_WAIT_SEC = 15 * 60
ROUND_GAP_SEC = 90


def snapshot(page, label: str) -> None:
    try:
        title = page.title()
    except Exception as exc:
        title = f"<title err: {exc}>"
    try:
        url = page.url
    except Exception as exc:
        url = f"<url err: {exc}>"
    print(
        f"[{label}] url={url!r} title={title!r} "
        f"blocked={is_blocked_page(page)} antibot={is_antibot_challenge_page(page)} "
        f"usable={page_has_usable_ozon_content(page)} "
        f"restricted={is_access_restricted(page)} incident={extract_incident_id(page)}"
    )


def wait_out_block(page, seconds: float) -> bool:
    """Passive wait while fab_/block is shown. Returns True if cleared."""
    print(f"Blocked — waiting up to {int(seconds // 60)} min without reload...")
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not is_blocked_page(page) and not is_access_restricted(page):
            if page_has_usable_ozon_content(page) or wait_for_ozon_ready(page, print, 30):
                print("Block cleared.")
                return True
        time.sleep(30)
        try:
            snapshot(page, "wait")
        except Exception as exc:
            print("wait check:", exc)
    return not is_access_restricted(page) and page_has_usable_ozon_content(page)


def run_once(page, mode: str) -> int:
    """0=ok, 2=blocked, 3=not ready, 4=nav fail, 5=parse fail, 6=few roots."""
    snapshot(page, "open")

    if is_blocked_page(page):
        if not wait_out_block(page, BLOCK_WAIT_SEC):
            print("STILL_BLOCKED", extract_incident_id(page))
            return 2

    ready = ensure_ozon_session_ready(
        page,
        print,
        warmup_url="https://www.ozon.ru/",
    )
    print("READY", ready)
    snapshot(page, "after_ensure")
    if not ready:
        if is_blocked_page(page) and wait_out_block(page, BLOCK_WAIT_SEC):
            ready = ensure_ozon_session_ready(page, print, warmup_url="https://www.ozon.ru/")
        if not ready:
            return 3

    # Prefer already-open usable page; only soft-goto seller if needed.
    if not page_has_usable_ozon_content(page) or "/seller/" not in (page.url or "").lower():
        ok = safe_goto(page, "https://www.ozon.ru/seller/", print, max_retries=1)
        snapshot(page, "goto:seller")
        if not ok or is_access_restricted(page):
            print("NAV_FAIL seller", extract_incident_id(page))
            return 4

    loader = CategoryLoader(page, DESKTOP_MODE, session_mode=mode)
    roots = loader._load_global_root_categories(print, None)
    print("ROOTS", len(roots))
    if roots:
        print("FIRST", roots[0].get("name"), roots[0].get("id"))
        kids = loader._fetch_categories_batch(
            "https://www.ozon.ru/category/",
            [str(roots[0]["id"])],
            print,
        )
        first_id = str(roots[0]["id"])
        print("CHILDREN", first_id, len(kids.get(first_id) or []))

    if len(roots) < 2:
        return 6

    # Soft parse-start probe after a short cool-down (mirrors app behaviour).
    print("Cool-down 40s before parse probe...")
    time.sleep(40)
    if is_access_restricted(page):
        print("Restricted before parse probe")
        return 5

    cid = str(roots[0]["id"])
    parse_url = f"https://www.ozon.ru/seller/?category={cid}&sorting=price"
    ok = safe_goto(page, parse_url, print, max_retries=1)
    snapshot(page, "parse_probe")
    if not ok or is_access_restricted(page):
        print("PARSE_PROBE_FAIL", extract_incident_id(page))
        return 5

    print("OK")
    return 0


def main() -> int:
    with sync_playwright() as pw:
        browser, context, page, mode = open_session_context(
            pw,
            headless=False,
            use_cdp=True,
            browser_mode=DESKTOP_MODE,
            use_auth=False,
            progress=print,
        )
        print("MODE", mode)

        try:
            for round_idx in range(1, MAX_ROUNDS + 1):
                print(f"\n===== ROUND {round_idx}/{MAX_ROUNDS} =====")
                code = run_once(page, mode)
                if code == 0:
                    return 0
                if code == 2:
                    print("Waiting for unban before next round...")
                    wait_out_block(page, BLOCK_WAIT_SEC)
                else:
                    print(f"Round failed with {code}; gap {ROUND_GAP_SEC}s...")
                    time.sleep(ROUND_GAP_SEC)
            return 1
        finally:
            close_session_context(browser, context, mode)


if __name__ == "__main__":
    raise SystemExit(main())
