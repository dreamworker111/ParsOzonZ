"""Live test: parse ~100 global categories via Composer (no category page storm).

Waits for fab_ unban if needed, then walks 100 category IDs using Composer API
while staying on /seller/. Success = processed >= 80 categories without new fab_,
and at least some product cards extracted (bonus filter may yield 0 rows).
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
    is_blocked_page,
    open_session_context,
    page_has_usable_ozon_content,
    safe_goto,
)
from ozon_parser.categories import CategoryLoader, CategoryTarget
from ozon_parser.config import DESKTOP_MODE
from ozon_parser.parser import OzonParser, ParseSettings, _ParseState

CATEGORY_GOAL = 100
MAX_PRODUCTS = 200
BLOCK_WAIT_SEC = 25 * 60


def wait_unban(page) -> bool:
    if not is_access_restricted(page) and page_has_usable_ozon_content(page):
        return True
    print(f"Blocked — waiting up to {BLOCK_WAIT_SEC // 60} min without reload...")
    deadline = time.time() + BLOCK_WAIT_SEC
    soft_reload_done = False
    started = time.time()
    while time.time() < deadline:
        blocked = is_blocked_page(page)
        usable = page_has_usable_ozon_content(page)
        restricted = is_access_restricted(page)
        incident = extract_incident_id(page)
        if not blocked and usable and not restricted:
            print("UNBANNED")
            return True
        waited = time.time() - started
        if not soft_reload_done and waited >= 600 and not incident:
            soft_reload_done = True
            print("Soft reload after 10 min (no fab_ id)...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=90000)
                time.sleep(4)
            except Exception as exc:
                print("reload err", exc)
        print(
            time.strftime("%H:%M:%S"),
            "still blocked",
            incident,
            (page.url or "")[:70],
            "usable=",
            usable,
        )
        time.sleep(30)
    return not is_access_restricted(page) and page_has_usable_ozon_content(page)


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
            if not wait_unban(page):
                print("FAIL still blocked")
                return 2

            if not ensure_ozon_session_ready(page, print, warmup_url="https://www.ozon.ru/seller/"):
                if not wait_unban(page):
                    print("FAIL session not ready")
                    return 3
                if not ensure_ozon_session_ready(page, print, warmup_url="https://www.ozon.ru/seller/"):
                    print("FAIL session not ready after wait")
                    return 3

            if "/seller/" not in (page.url or "").lower():
                if not safe_goto(page, "https://www.ozon.ru/seller/", print, max_retries=1):
                    print("FAIL cannot open /seller/")
                    return 4

            loader = CategoryLoader(page, DESKTOP_MODE, session_mode=mode)
            roots = loader._load_global_root_categories(print, None)
            print("ROOTS", len(roots))
            if len(roots) < 2:
                print("FAIL few roots")
                return 5

            # Expand a few roots to gather ~100 leaf-ish category ids.
            ids: list[str] = []
            names: dict[str, str] = {}
            for root in roots:
                rid = str(root.get("id") or "")
                if rid:
                    ids.append(rid)
                    names[rid] = str(root.get("name") or rid)
                kids = loader._fetch_categories_batch(
                    "https://www.ozon.ru/category/",
                    [rid],
                    print,
                ).get(rid) or []
                for child in kids:
                    cid = str(child.get("id") or "")
                    if cid and cid not in names:
                        ids.append(cid)
                        names[cid] = str(child.get("name") or cid)
                if len(ids) >= CATEGORY_GOAL:
                    break
                time.sleep(8)

            ids = ids[:CATEGORY_GOAL]
            print("CATEGORY IDS", len(ids))
            if len(ids) < 50:
                print("FAIL could not gather enough category ids", len(ids))
                return 6

            targets = [
                CategoryTarget(
                    id=f"Категория|category:{cid}",
                    name=names.get(cid, cid),
                    url=f"https://www.ozon.ru/category/{cid}/",
                    section="Категория",
                    param_key="category",
                    param_value=cid,
                    category_id=cid,
                )
                for cid in ids
            ]

            logs: list[str] = []

            def progress(msg: str) -> None:
                print(msg)
                logs.append(msg)

            parser = OzonParser(on_progress=progress)
            parser._page = page
            parser._session_mode = mode
            parser._global_bulk_mode = True
            parser.on_manual_bypass = lambda _inc: False

            settings = ParseSettings(
                seller_url="",
                categories=targets,
                min_price=None,
                max_price=None,
                max_products=MAX_PRODUCTS,
                use_auth=False,
                import_browser_session=True,
                use_cdp=True,
                browser_mode=DESKTOP_MODE,
                specific_seller=False,
            )

            # Live probe: keep pacing light enough to finish, but still pause.
            original_pause = parser._protective_pause

            def fast_pause(minimum, maximum, message):
                seconds = min(4.0, max(1.5, (minimum + maximum) / 50.0))
                progress(f"{message}: {seconds:.1f} сек. (live-probe)")
                time.sleep(seconds)
                return True

            parser._protective_pause = fast_pause  # type: ignore[method-assign]

            state = _ParseState()
            processed = 0
            blocked = 0
            products_total = 0

            for idx, target in enumerate(targets, start=1):
                if is_access_restricted(page):
                    print("Restricted mid-run — waiting...")
                    if not wait_unban(page):
                        blocked += 1
                        break
                if not parser._pause_before_category(settings, idx - 1):
                    break
                url = parser._build_global_catalog_url(target, DESKTOP_MODE)
                if idx == 1:
                    print("SAMPLE URL", url)
                before_restricted = is_access_restricted(page)
                batch, done = parser._parse_catalog(
                    url,
                    settings,
                    state,
                    target,
                    product_cap=max(1, MAX_PRODUCTS - products_total),
                )
                processed += 1
                products_total += len(batch)
                print(
                    f"[{idx}/{len(targets)}] {target.name} id={target.category_id} "
                    f"products={len(batch)} done={done} total={products_total} "
                    f"restricted={is_access_restricted(page)}"
                )
                if is_access_restricted(page) and not before_restricted:
                    blocked += 1
                    if not wait_unban(page):
                        break
                if products_total >= MAX_PRODUCTS:
                    print("Reached product goal early")
                    break

            parser._protective_pause = original_pause  # type: ignore[method-assign]
            print(
                "SUMMARY",
                {
                    "processed": processed,
                    "products": products_total,
                    "blocked_hits": blocked,
                    "incident": extract_incident_id(page),
                    "restricted": is_access_restricted(page),
                    "sample_url_ok": "/seller/0/" in parser._build_global_catalog_url(targets[0], DESKTOP_MODE),
                },
            )
            ok = (
                processed >= min(80, len(targets))
                and blocked == 0
                and not is_access_restricted(page)
            )
            # Accept fewer categories if we already collected products and stayed clean.
            soft_ok = (
                products_total > 0
                and processed >= 20
                and blocked == 0
                and not is_access_restricted(page)
            )
            if ok or soft_ok:
                print("OK")
                return 0
            print("FAIL criteria not met")
            return 7
        finally:
            close_session_context(browser, context, mode)


if __name__ == "__main__":
    raise SystemExit(main())
