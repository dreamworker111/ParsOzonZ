"""Live probe: fast category tree load + electronics parse without fab_ storm."""

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

ELECTRONICS_ID = "15500"
MAX_LEAF = 40
MAX_PRODUCTS = 30
BLOCK_WAIT = 20 * 60


def wait_unban(page) -> bool:
    if page_has_usable_ozon_content(page) and not is_access_restricted(page):
        return True
    print("Waiting for unban...")
    deadline = time.time() + BLOCK_WAIT
    reloaded = False
    started = time.time()
    while time.time() < deadline:
        if page_has_usable_ozon_content(page) and not is_access_restricted(page):
            print("UNBANNED")
            return True
        if not reloaded and time.time() - started > 600 and not extract_incident_id(page):
            reloaded = True
            try:
                page.reload(wait_until="domcontentloaded", timeout=90000)
            except Exception as exc:
                print("reload", exc)
        print(time.strftime("%H:%M:%S"), "blocked", extract_incident_id(page))
        time.sleep(20)
    return page_has_usable_ozon_content(page) and not is_access_restricted(page)


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
        try:
            if not wait_unban(page):
                print("FAIL still blocked")
                return 2
            if not ensure_ozon_session_ready(page, print, warmup_url="https://www.ozon.ru/seller/"):
                print("FAIL session")
                return 3
            if "/seller/" not in (page.url or ""):
                safe_goto(page, "https://www.ozon.ru/seller/", print, max_retries=1)

            loader = CategoryLoader(page, DESKTOP_MODE, session_mode=mode)
            t0 = time.time()
            # Speed probe: only electronics subtree, not full catalog.
            roots = loader._load_global_root_categories(print, None)
            electronics = next(
                (r for r in roots if str(r.get("id")) == ELECTRONICS_ID),
                None,
            )
            if not electronics:
                print("FAIL no electronics root", [r.get("name") for r in roots[:5]])
                return 4
            kids = loader._fetch_categories_batch(
                "https://www.ozon.ru/category/",
                [ELECTRONICS_ID],
                print,
            )
            if kids is None:
                print("FAIL batch blocked")
                return 5
            children = kids.get(ELECTRONICS_ID) or []
            electronics["children"] = children
            loader._complete_category_subtree(
                "https://www.ozon.ru/category/",
                electronics["children"],
                print,
                deadline=time.monotonic() + 180,
            )
            elapsed = time.time() - t0
            leaf_ids: list[tuple[str, str]] = []

            def walk(node: dict, depth: int = 0) -> None:
                kids_local = node.get("children") or []
                if not kids_local:
                    leaf_ids.append((str(node.get("id")), str(node.get("name"))))
                    return
                for child in kids_local:
                    walk(child, depth + 1)

            walk(electronics)
            print(
                "TREE",
                {
                    "seconds": round(elapsed, 1),
                    "direct_children": len(children),
                    "leaves": len(leaf_ids),
                    "restricted": is_access_restricted(page),
                },
            )
            if is_access_restricted(page):
                print("FAIL blocked during tree")
                return 6
            if elapsed > 120:
                print("WARN tree slow", elapsed)

            targets = [
                CategoryTarget(
                    id=f"Категория|category:{cid}",
                    name=name,
                    url=f"https://www.ozon.ru/category/{cid}/",
                    section="Категория",
                    param_key="category",
                    param_value=cid,
                    category_id=cid,
                )
                for cid, name in leaf_ids[:MAX_LEAF]
            ]
            print("PARSE TARGETS", len(targets))

            logs: list[str] = []

            def progress(msg: str) -> None:
                print(msg)
                logs.append(msg)

            parser = OzonParser(on_progress=progress)
            parser._page = page
            parser._session_mode = mode
            parser._global_bulk_mode = True
            parser.on_manual_bypass = lambda _i: False

            def fast_pause(a, b, message):
                progress(f"{message}: 1 сек (probe)")
                time.sleep(1.0)
                return True

            parser._protective_pause = fast_pause  # type: ignore
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
            state = _ParseState()
            total = 0
            blocked = 0
            for idx, target in enumerate(targets, start=1):
                if is_access_restricted(page):
                    blocked += 1
                    break
                if not parser._pause_before_category(settings, idx - 1):
                    break
                url = parser._build_global_catalog_url(target, DESKTOP_MODE)
                batch, _done = parser._parse_catalog(
                    url,
                    settings,
                    state,
                    target,
                    product_cap=max(1, MAX_PRODUCTS - total),
                )
                total += len(batch)
                print(
                    f"[{idx}/{len(targets)}] {target.name} +{len(batch)} "
                    f"total={total} restricted={is_access_restricted(page)}"
                )
                if is_access_restricted(page):
                    blocked += 1
                    break
                if total >= MAX_PRODUCTS:
                    break

            print(
                "SUMMARY",
                {
                    "products": total,
                    "blocked": blocked,
                    "tree_sec": round(elapsed, 1),
                    "incident": extract_incident_id(page),
                },
            )
            ok = blocked == 0 and not is_access_restricted(page) and (
                total > 0 or len(targets) >= 10
            )
            # Prefer finding at least one bonus product when DOM passes ran.
            if ok:
                print("OK")
                return 0
            print("FAIL")
            return 7
        finally:
            close_session_context(browser, context, mode)


if __name__ == "__main__":
    raise SystemExit(main())
