"""Live probe: Accessories → Women's → Hair accessories depth."""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from ozon_parser.browser import (
    connect_via_cdp,
    is_access_restricted,
    is_blocked_page,
    wait_for_ozon_ready,
)
from ozon_parser.categories import CategoryLoader


def walk_nodes(nodes: list[dict], depth: int = 0) -> tuple[int, int, list[tuple[int, str, int]]]:
    total = 0
    max_d = depth
    leaves: list[tuple[int, str, int]] = []
    for node in nodes:
        total += 1
        children = node.get("children") or []
        name = str(node.get("name") or node.get("id") or "")
        if not children:
            leaves.append((depth, name, int(str(node.get("id") or "0") or 0)))
        for child in children:
            t, m, lv = walk_nodes([child], depth + 1)
            total += t
            max_d = max(max_d, m)
            leaves.extend(lv)
    return total, max_d, leaves


def find_path(nodes: list[dict], *names: str, depth: int = 0) -> dict | None:
    if not names:
        return None
    target = names[0].lower()
    for node in nodes:
        name = str(node.get("name") or "").lower()
        if target in name:
            if len(names) == 1:
                return node
            found = find_path(node.get("children") or [], *names[1:], depth=depth + 1)
            if found:
                return found
    return None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with sync_playwright() as pw:
        _b, _c, page = connect_via_cdp(pw)
        print("blocked", is_blocked_page(page), "restricted", is_access_restricted(page))
        if not wait_for_ozon_ready(page, print):
            print("FAIL ready")
            return 2

        loader = CategoryLoader(page)
        if "ozon.ru" not in str(page.url or "").lower():
            page.goto("https://www.ozon.ru/", wait_until="domcontentloaded", timeout=60000)
            wait_for_ozon_ready(page, print)

        raw_roots = loader._load_global_root_categories(print, None)
        accessories = next(
            (r for r in raw_roots if "аксессуар" in str(r.get("name", "")).lower()),
            None,
        )
        if not accessories:
            print("FAIL root not found", [r.get("name") for r in raw_roots[:20]])
            return 1
        root_id = str(accessories["id"])
        print("ROOT", root_id, accessories.get("name"))

        root = {
            "id": root_id,
            "name": str(accessories.get("name") or root_id),
            "url": accessories.get("url"),
            "children": [],
        }
        loader._seller_root_ids = {root_id}
        loader._collect_category_branch_whole(
            "https://www.ozon.ru/category/",
            root,
            log=print,
            on_manual_bypass=None,
        )

        total, depth, leaves = walk_nodes(root.get("children") or [], 1)
        print(f"RESULT nodes={total} max_depth={depth} root_children={len(root.get('children') or [])}")

        hair = find_path(root.get("children") or [], "женск", "волос")
        if hair:
            hair_children = hair.get("children") or []
            print(
                "HAIR",
                hair.get("id"),
                hair.get("name"),
                "children",
                len(hair_children),
                [c.get("name") for c in hair_children[:15]],
            )
            ok = len(hair_children) >= 1 and depth >= 4
        else:
            print("FAIL hair branch not found")
            ok = False

        deep_leaves = [lv for lv in leaves if lv[0] >= 3]
        print("deep_leaves_sample", deep_leaves[:20])
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
