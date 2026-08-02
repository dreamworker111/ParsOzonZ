import json
import random
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .config import (
    BrowserMode,
    DESKTOP_MODE,
    DELAY_BETWEEN_CATEGORIES_MAX,
    DELAY_BETWEEN_CATEGORIES_MIN,
    DELAY_CLICK_MAX,
    DELAY_CLICK_MIN,
    DELAY_PAGE_MAX,
    DELAY_PAGE_MIN,
    DELAY_SCROLL_MAX,
    DELAY_SCROLL_MIN,
    MOBILE_MODE,
)


def human_delay(min_sec: float | None = None, max_sec: float | None = None) -> None:
    lo = min_sec if min_sec is not None else DELAY_PAGE_MIN
    hi = max_sec if max_sec is not None else DELAY_PAGE_MAX
    time.sleep(random.uniform(lo, hi))


def human_scroll_delay() -> None:
    time.sleep(random.uniform(DELAY_SCROLL_MIN, DELAY_SCROLL_MAX))


def human_category_delay() -> None:
    time.sleep(random.uniform(DELAY_BETWEEN_CATEGORIES_MIN, DELAY_BETWEEN_CATEGORIES_MAX))


def human_click_delay() -> None:
    time.sleep(random.uniform(DELAY_CLICK_MIN, DELAY_CLICK_MAX))


def fast_delay(min_sec: float = 0.15, max_sec: float = 0.35) -> None:
    time.sleep(random.uniform(min_sec, max_sec))


def to_desktop_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.replace("m.ozon.ru", "www.ozon.ru")
    if "ozon.ru" not in host:
        host = "www.ozon.ru"
    return urlunparse((parsed.scheme or "https", host, parsed.path, parsed.params, parsed.query, parsed.fragment))


def with_price_sort_asc(
    url: str,
    browser_mode: BrowserMode = DESKTOP_MODE,
    session_mode: str | None = None,
) -> str:
    """Добавляет сортировку «по возрастанию цены» к URL каталога Ozon."""
    url = route_browser_url(url, browser_mode, session_mode)
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["sorting"] = "price"
    query = urlencode(params)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))


def to_mobile_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.ozon.ru", "m.ozon.ru")
    if "ozon.ru" not in host:
        host = "m.ozon.ru"
    return urlunparse((parsed.scheme or "https", host, parsed.path, parsed.params, parsed.query, parsed.fragment))


def to_browser_url(url: str, browser_mode: BrowserMode = DESKTOP_MODE) -> str:
    """Route an Ozon URL to the host used by the selected browser mode."""
    if browser_mode == DESKTOP_MODE:
        return to_desktop_url(url)
    if browser_mode == MOBILE_MODE:
        return to_mobile_url(url)
    raise ValueError(f"Неизвестный режим браузера: {browser_mode}")


def route_browser_url(
    url: str,
    browser_mode: BrowserMode = DESKTOP_MODE,
    session_mode: str | None = None,
) -> str:
    """Route URLs for mobile UI sessions that still browse via www.ozon.ru."""
    from .config import MOBILE_DESKTOP_HOST_SESSIONS, MOBILE_MODE

    if browser_mode == MOBILE_MODE and session_mode in MOBILE_DESKTOP_HOST_SESSIONS:
        return to_desktop_url(url)
    return to_browser_url(url, browser_mode)


def to_desktop_product_url(url: str) -> str:
    url = url.strip()
    if url.startswith("/"):
        url = "https://www.ozon.ru" + url
    return url.replace("m.ozon.ru", "www.ozon.ru")


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", text.replace("\u2009", "").replace("\xa0", " "))
    if not cleaned:
        return None
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def has_bonus_text(text: str) -> bool:
    if not text:
        return False
    lower = text.lower().replace("ё", "е")
    if "балл" in lower and "отзыв" in lower:
        return True
    if "бонус" in lower and "отзыв" in lower:
        return True
    if "баллы за отзыв" in lower or "баллов за отзыв" in lower:
        return True
    return False


def parse_bonus_points(text: str | None) -> int | None:
    if not text:
        return None
    lower = text.lower().replace("ё", "е")
    for pattern in (
        r"(\d[\d\s]*)\s*балл",
        r"(\d[\d\s]*)\s*бонус",
        r"\+?\s*(\d[\d\s]*)\s*(?:₽\s*)?за\s*отзыв",
    ):
        match = re.search(pattern, lower)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                return int(digits)
    if has_bonus_text(text):
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else None
    return None


def price_diff(original: float, discounted: float) -> tuple[float, float]:
    diff = original - discounted
    if original <= 0:
        return diff, 0.0
    return diff, round((diff / original) * 100, 2)


def extract_json_blocks(html: str) -> list[Any]:
    results: list[Any] = []
    for pattern in (
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
        r"window\.__NUXT__\s*=\s*(.*?);</script>",
        r"window\.__INITIAL_STATE__\s*=\s*(.*?);</script>",
    ):
        for match in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
            raw = match.group(1).strip()
            try:
                results.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return results


def find_category_nodes(obj: Any, found: list[dict] | None = None) -> list[dict]:
    """Recursively locate category-like dicts in arbitrary Ozon JSON."""
    if found is None:
        found = []

    if isinstance(obj, dict):
        name = obj.get("title") or obj.get("name") or obj.get("caption") or obj.get("text")
        cid = obj.get("id") or obj.get("categoryId") or obj.get("category_id") or obj.get("value")
        children = obj.get("children") or obj.get("categories") or obj.get("nodes") or obj.get("items")

        if name and cid and isinstance(name, str):
            node = {"id": str(cid), "name": name.strip(), "children": []}
            if children and isinstance(children, list):
                for child in children:
                    child_nodes = find_category_nodes(child, [])
                    if child_nodes:
                        node["children"].extend(child_nodes)
            if not any(n["id"] == node["id"] for n in found):
                found.append(node)

        for value in obj.values():
            find_category_nodes(value, found)

    elif isinstance(obj, list):
        for item in obj:
            find_category_nodes(item, found)

    return found


def merge_category_trees(trees: list[list[dict]]) -> list[dict]:
    merged: dict[str, dict] = {}

    def add_node(node: dict, parent: dict | None = None) -> None:
        nid = node["id"]
        if nid not in merged:
            merged[nid] = {"id": nid, "name": node["name"], "children": []}
        target = merged[nid]
        for child in node.get("children", []):
            add_node(child, target)
            child_id = child["id"]
            if not any(c["id"] == child_id for c in target["children"]):
                target["children"].append(merged[child_id])

    for tree in trees:
        for root in tree:
            add_node(root)

    roots = [n for n in merged.values() if not any(
        any(c["id"] == n["id"] for c in p.get("children", [])) for p in merged.values()
    )]
    return roots or list(merged.values())


def normalize_seller_url(
    url: str,
    browser_mode: BrowserMode = DESKTOP_MODE,
    session_mode: str | None = None,
) -> str:
    routed = route_browser_url(url, browser_mode, session_mode)
    if "/seller/" not in routed:
        raise ValueError("Ссылка должна вести на страницу магазина Ozon (содержать /seller/)")
    return routed.rstrip("/") + "/"
