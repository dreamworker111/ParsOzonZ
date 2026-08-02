"""Parse Ozon Composer API, HTML and embedded JSON into category trees."""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

from .config import CATEGORY_BLOCK_CLASS
from .utils import is_valid_category


def extract_json_blocks(html: str) -> list[Any]:
    results: list[Any] = []
    patterns = (
        r'<script[^>]*type="application/json"[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>',
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
        r"window\.__NUXT__\s*=\s*(.*?);</script>",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
            raw = match.group(1).strip()
            try:
                results.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return results


def _category_from_dict(obj: dict[str, Any]) -> dict[str, Any] | None:
    name = (
        obj.get("title")
        or obj.get("name")
        or obj.get("caption")
        or obj.get("text")
        or obj.get("label")
    )
    cid = obj.get("categoryId") or obj.get("category_id") or obj.get("id") or obj.get("value")

    if isinstance(name, dict):
        name = name.get("text") or name.get("title")
    if not name or cid is None or not isinstance(name, str):
        return None

    name = name.strip()
    cid = str(cid).strip()
    if not is_valid_category(name, cid):
        return None

    child_keys = ("children", "categories", "nodes", "items", "sections", "subCategories", "subcategories")
    children: list[dict[str, Any]] = []
    for key in child_keys:
        val = obj.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    child = _normalize_category_item(item)
                    if child:
                        children.append(child)

    url = obj.get("url") or obj.get("link") or obj.get("deeplink")
    return {"id": cid, "name": name, "url": str(url) if url else None, "children": children}


def _normalize_category_item(obj: dict[str, Any]) -> dict[str, Any] | None:
    node = _category_from_dict(obj)
    if node:
        return node

    link = obj.get("link") or obj.get("deeplink") or obj.get("url") or ""
    name = obj.get("title") or obj.get("text") or obj.get("name") or ""
    if not isinstance(name, str):
        return None
    name = name.strip()

    match = re.search(r"[?&]category=(\d+)", str(link), re.I)
    if not match:
        match = re.search(r"category[=/](\d+)", str(link), re.I)
    if match and is_valid_category(name, match.group(1)):
        return {"id": match.group(1), "name": name, "url": str(link) or None, "children": []}
    return None


def find_category_nodes(obj: Any, found: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if found is None:
        found = []

    if isinstance(obj, dict):
        node = _normalize_category_item(obj)
        if node and not any(n["id"] == node["id"] for n in found):
            found.append(node)
        for value in obj.values():
            find_category_nodes(value, found)
    elif isinstance(obj, list):
        for item in obj:
            find_category_nodes(item, found)

    return found


def _build_tree(flat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not flat:
        return []

    merged: dict[str, dict[str, Any]] = {}
    for item in flat:
        if not is_valid_category(item["name"], item["id"]):
            continue
        cid = str(item["id"])
        if cid not in merged:
            merged[cid] = {"id": cid, "name": item["name"], "url": item.get("url"), "children": []}
        elif item.get("url") and not merged[cid].get("url"):
            merged[cid]["url"] = item["url"]
        for child in item.get("children", []):
            if not is_valid_category(child["name"], child["id"]):
                continue
            child_id = str(child["id"])
            if child_id not in merged:
                merged[child_id] = {
                    "id": child_id,
                    "name": child["name"],
                    "url": child.get("url"),
                    "children": [],
                }
            elif child.get("url") and not merged[child_id].get("url"):
                merged[child_id]["url"] = child["url"]
            if not any(c["id"] == child_id for c in merged[cid]["children"]):
                merged[cid]["children"].append(merged[child_id])

    child_ids = {c["id"] for node in merged.values() for c in node.get("children", [])}
    roots = [node for cid, node in merged.items() if cid not in child_ids]
    return roots or list(merged.values())


def parse_composer_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract category nodes from Composer API widgetStates."""
    categories: list[dict[str, Any]] = []
    widget_states = data.get("widgetStates") or {}

    for key, raw in widget_states.items():
        key_lower = key.lower()
        if not any(token in key_lower for token in ("filter", "category", "catalog")):
            continue
        try:
            state = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        categories.extend(find_category_nodes(state))

    return _build_tree(categories)


def _find_node(nodes: list[dict[str, Any]], category_id: str) -> dict[str, Any] | None:
    for node in nodes:
        if str(node.get("id")) == category_id:
            return node
        found = _find_node(node.get("children") or [], category_id)
        if found:
            return found
    return None


def extract_direct_children(
    tree: list[dict[str, Any]],
    parent_id: str,
    *,
    root_ids: set[str] | None = None,
    page_category_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return direct children of parent_id from a parsed category tree."""
    parent_id = str(parent_id)
    root_ids = root_ids or set()

    parent = _find_node(tree, parent_id)
    if parent:
        children = parent.get("children") or []
        if children:
            return [
                {"id": str(c["id"]), "name": c["name"], "url": c.get("url"), "children": []}
                for c in children
                if is_valid_category(str(c.get("name", "")), str(c.get("id", "")))
            ]

    # If the parent is absent, the response does not prove a relationship.
    # Treating every other category as its child duplicates roots in branches.
    return []


def parse_html_category_block(html: str, parent_id: str | None = None) -> list[dict[str, Any]]:
    """Fallback: parse wb6_7 filter block from HTML."""
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.select(f'[class*="{CATEGORY_BLOCK_CLASS}"]')
    if not blocks:
        for widget in soup.select('[data-widget="filtersDesktop"], [data-widget="searchFilters"]'):
            blocks.extend(widget.select(f'[class*="{CATEGORY_BLOCK_CLASS}"]'))

    best = max(blocks, key=lambda b: len(b.select('a[href*="category="]')), default=None)
    if best is None:
        best = soup.select_one('[data-widget="filtersDesktop"]') or soup.select_one(
            '[data-widget="searchFilters"]'
        )
    if best is None:
        return []

    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in best.select('a[href*="category="]'):
        href = a.get("href") or ""
        m = re.search(r"[?&]category=(\d+)", href, re.I)
        if not m:
            continue
        cid = m.group(1)
        if cid in seen:
            continue
        name = (a.get_text(strip=True) or a.get("title") or "").strip()
        if not name or not is_valid_category(name, cid):
            continue
        seen.add(cid)
        depth = 0
        parent = a.parent
        while parent is not None and parent is not best:
            depth += 1
            parent = parent.parent
        links.append({"id": cid, "name": name, "url": href, "depth": depth})

    if not links:
        return []

    if not parent_id:
        min_depth = min(item["depth"] for item in links)
        return [
            {"id": item["id"], "name": item["name"], "url": item["url"], "children": []}
            for item in links
            if item["depth"] == min_depth
        ]

    parent_idx = next((i for i, item in enumerate(links) if item["id"] == parent_id), -1)
    if parent_idx >= 0:
        p_depth = links[parent_idx]["depth"]
        out: list[dict[str, Any]] = []
        for item in links[parent_idx + 1 :]:
            if item["depth"] <= p_depth:
                break
            if item["depth"] == p_depth + 1:
                out.append({"id": item["id"], "name": item["name"], "url": item["url"], "children": []})
        if out:
            return out

    # Do not infer children from unrelated links when the parent is absent.
    return []


def parse_page_all_sources(composer: dict[str, Any] | None, html: str | None, parent_id: str | None = None) -> list[dict[str, Any]]:
    """Merge composer + HTML parsing for robustness."""
    trees: list[list[dict[str, Any]]] = []
    if composer:
        trees.append(parse_composer_response(composer))
    if html:
        trees.append(parse_html_category_block(html, parent_id))

    if not trees:
        return []

    merged = _build_tree([node for tree in trees for node in tree])
    if parent_id:
        return extract_direct_children(merged, parent_id, page_category_id=parent_id)
    return merged
