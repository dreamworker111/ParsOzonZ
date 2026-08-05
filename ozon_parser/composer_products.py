"""Extract product listing cards from Ozon Composer API JSON without page navigation."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from ozon_parser.utils import extract_ozon_product_id, pick_product_name


_PRODUCT_HREF_RE = re.compile(r"/product/[^\"'\s?#]+", re.I)


def _as_mapping(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _iter_nodes(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_nodes(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_nodes(item)
    elif isinstance(obj, str) and len(obj) > 20 and ("{" in obj or "[" in obj):
        try:
            parsed = json.loads(obj)
        except Exception:
            return
        yield from _iter_nodes(parsed)


def _texts_from_main_state(main_state: Any) -> list[str]:
    texts: list[str] = []
    if not isinstance(main_state, list):
        return texts
    for block in main_state:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype == "priceV2":
            price = ((block.get("priceV2") or {}).get("price") or [])
            for item in price:
                if isinstance(item, dict) and item.get("text"):
                    texts.append(str(item["text"]))
        elif btype == "textDS":
            text = (block.get("textDS") or {}).get("text")
            if text:
                texts.append(str(text))
        elif btype in {"labelListV2", "labelList"}:
            root = block.get("labelListV2") or block.get("labelList") or {}
            for item in root.get("items") or []:
                if not isinstance(item, dict):
                    continue
                text = ((item.get("text") or {}).get("text") if isinstance(item.get("text"), dict) else item.get("text"))
                if text:
                    texts.append(str(text))
        else:
            blob = json.dumps(block, ensure_ascii=False)
            for match in re.findall(r"\"text\"\s*:\s*\"([^\"]{2,120})\"", blob):
                texts.append(match)
    return texts


def _card_from_tile_item(item: dict, *, base_url: str) -> dict | None:
    action = item.get("action") or {}
    href = ""
    if isinstance(action, dict):
        href = str(action.get("link") or "")
    if "/product/" not in href:
        match = _PRODUCT_HREF_RE.search(json.dumps(item, ensure_ascii=False))
        if not match:
            return None
        href = match.group(0)
    href = href.split("?")[0].split("#")[0]
    if href.startswith("/"):
        href = urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))

    texts = _texts_from_main_state(item.get("mainState"))
    name = pick_product_name(*texts)
    body = "\n".join(texts)
    if not body:
        body = name
    return {
        "href": href,
        "name": name,
        "text": body,
        "html": body,
    }


def _extract_tile_grid_cards(data: dict, *, base_url: str) -> list[dict]:
    cards: list[dict] = []
    seen: set[str] = set()
    widget_states = data.get("widgetStates") or {}
    if not isinstance(widget_states, dict):
        return cards
    for key, raw in widget_states.items():
        if "tilegrid" not in str(key).lower() and "searchresults" not in str(key).lower():
            # Still parse known product grids even with odd names.
            mapped = _as_mapping(raw)
            if not mapped or not isinstance(mapped.get("items"), list):
                continue
        else:
            mapped = _as_mapping(raw)
        if not mapped:
            continue
        items = mapped.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            # tileGridDesktop product tiles have sku/mainState/action.
            if not (item.get("sku") or item.get("mainState") or item.get("action")):
                continue
            card = _card_from_tile_item(item, base_url=base_url)
            if not card:
                continue
            href = card["href"]
            key = extract_ozon_product_id(href) or href
            if key in seen:
                continue
            seen.add(key)
            cards.append(card)
    return cards


def extract_product_cards_from_composer(
    data: dict | None,
    *,
    base_url: str = "https://www.ozon.ru",
) -> list[dict]:
    """Return card dicts compatible with OzonParser._process_card."""
    if not isinstance(data, dict):
        return []

    cards = _extract_tile_grid_cards(data, base_url=base_url)
    if cards:
        return cards

    # Generic fallback for unexpected layouts.
    seen: set[str] = set()
    out: list[dict] = []
    for node in _iter_nodes(data):
        if not isinstance(node, dict):
            continue
        action = node.get("action")
        href = ""
        if isinstance(action, dict):
            href = str(action.get("link") or "")
        if "/product/" not in href:
            continue
        href = href.split("?")[0].split("#")[0]
        if href.startswith("/"):
            href = urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))
        key = extract_ozon_product_id(href) or href
        if key in seen:
            continue
        texts = _texts_from_main_state(node.get("mainState"))
        name = pick_product_name(*texts)
        body = "\n".join(texts) or name
        if "₽" not in body and not name:
            continue
        seen.add(key)
        out.append({"href": href, "name": name, "text": body, "html": body})
    return out


def composer_fetch_script() -> str:
    return """
    async ({ path, fullUrl, clientName }) => {
        const candidates = [
            '/api/composer-api.bx/page/json/v2?url=' + encodeURIComponent(path),
            '/api/composer-api.bx/page/json/v2?url=' + encodeURIComponent(fullUrl),
        ];
        for (const apiUrl of candidates) {
            try {
                const resp = await fetch(apiUrl, {
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json',
                        'x-o3-app-name': clientName,
                    },
                });
                if (!resp.ok) continue;
                return JSON.stringify(await resp.json());
            } catch (e) {}
        }
        return null;
    }
    """
