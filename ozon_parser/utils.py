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


def extract_ozon_category_id(*candidates: object) -> str:
    """Pull a numeric Ozon category id from ids, query params, or slug URLs."""
    for raw in candidates:
        text = str(raw or "").strip()
        if not text:
            continue
        if re.fullmatch(r"\d{2,}", text):
            return text
        match = re.search(r"[?&]category=(\d{2,})\b", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(
            r"/category/[^/?#]*-(\d{2,})(?:[/?#]|$)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)
        match = re.search(r"/category/(\d{2,})(?:[/?#]|$)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"category:(\d{2,})\b", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def with_price_sort_asc(
    url: str,
    browser_mode: BrowserMode = DESKTOP_MODE,
    session_mode: str | None = None,
) -> str:
    """Добавляет сортировку «по возрастанию цены» к URL каталога Ozon."""
    return sanitize_catalog_url(
        url,
        browser_mode=browser_mode,
        session_mode=session_mode,
        keep_sorting=True,
    )


def sanitize_catalog_url(
    url: str,
    *,
    param_key: str = "",
    param_value: str = "",
    category_id: str = "",
    browser_mode: BrowserMode = DESKTOP_MODE,
    session_mode: str | None = None,
    keep_sorting: bool = True,
) -> str:
    """Rebuild a seller/catalog URL with only the intended filter parameters.

    Ozon filter links often carry stale query flags (opened, layout, page, …) that
    lead to «Не нашли товары / Сбросить фильтры» empty states.
    """
    url = route_browser_url(url, browser_mode, session_mode)
    parsed = urlparse(url)
    path_lower = (parsed.path or "").lower()
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))

    params: dict[str, str] = {}
    category = str(category_id or "").strip()
    key = str(param_key or "").strip()
    value = str(param_value or "").strip()

    if key == "category" and value:
        category = value
    elif not category:
        category = str(existing.get("category") or "").strip()

    if category and "/seller/" in path_lower:
        params["category"] = category

    if key and value and key != "category":
        params[key] = value

    if keep_sorting:
        params["sorting"] = "price"

    query = urlencode(params)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment)
    )


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


def extract_ozon_product_id(url: str | None) -> str:
    """Numeric Ozon product id from a /product/... URL (same item, different slugs)."""
    if not url:
        return ""
    path = urlparse(to_desktop_product_url(str(url))).path or ""
    match = re.search(r"/product/(?:[^/]*?-)?(\d{6,})/?(?:$|[?#])", path, re.I)
    if match:
        return match.group(1)
    match = re.search(r"/product/[^/?#]*?(\d{6,})", path, re.I)
    return match.group(1) if match else ""


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
    # Ozon often shows "200 ₽ за отзыв" without the word "балл".
    if re.search(r"за\s*отзыв", lower):
        if re.search(r"\d", lower) or "₽" in lower or "руб" in lower:
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


# Listing badges / stock / promo labels that must not become the product title.
_PRODUCT_NAME_NOISE_FULL = re.compile(
    r"(?is)^(?:"
    r"\d+\s*шт\.?"
    r"|осталось\s+\d+\s*шт\.?"
    r"|распродажа"
    r"|суперцена"
    r"|хит(?:\s*продаж)?"
    r"|новинка"
    r"|акция"
    r"|скидка"
    r"|оригинал"
    r"|premium"
    r"|бренд\s+проверен"
    r"|проверенн?ый\s+бренд"
    r"|бренд\s+подтвержд[её]н"
    r"|официальный\s+бренд"
    r"|выбор\s+покупателей?"
    r"|цена\s+что\s+надо"
    r"|ценопад"
    r"|в\s+корзину"
    r"|в\s+избранное"
    r"|купить"
    r"|завтра"
    r"|сегодня"
    r"|послезавтра"
    r"|бесплатная\s+доставка"
    r"|доставка\s+\S+"
    r"|курьером"
    r"|[\d\s]+[-–—]?\s*(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*"
    r"|[-−+]?\d+\s*%"
    r"|\d([.,]\d)?\s*(?:★|⭐)?"
    r"|\d+\s*(?:отзыв|оценк)\w*"
    r"|цвет\s*:.+"
    r"|размер\s*:.+"
    r")$",
)

_PRODUCT_NAME_NOISE_INLINE = re.compile(
    r"(?is)(?:^|\s+)(?:"
    r"\d+\s*шт\.?"
    r"|осталось\s+\d+\s*шт\.?"
    r"|распродажа"
    r"|суперцена"
    r"|хит(?:\s*продаж)?"
    r"|новинка"
    r"|акция"
    r"|ценопад"
    r"|выбор\s+покупателей?"
    r"|бренд\s+проверен"
    r"|проверенн?ый\s+бренд"
    r"|бренд\s+подтвержд[её]н"
    r"|официальный\s+бренд"
    r"|оригинал"
    r"|[-−+]\d+\s*%"
    r")(?=\s|$)",
)


def is_product_name_noise(text: str | None) -> bool:
    """True for promo/stock/delivery badges mistaken for a product title."""
    if not text:
        return True
    value = " ".join(str(text).split()).strip(" ·•|-–—")
    if not value:
        return True
    if len(value) < 3:
        return True
    if re.search(r"\d[\d\s\u2009]*\s*₽", value):
        return True
    if has_bonus_text(value):
        return True
    if _PRODUCT_NAME_NOISE_FULL.match(value):
        return True
    lowered = value.lower()
    if "бренд" in lowered and any(
        marker in lowered for marker in ("провер", "подтвержд", "официал")
    ):
        return True
    # Mostly digits / punctuation, no real words.
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", value)
    if len(letters) < 3 and not re.search(r"[A-Za-zА-Яа-яЁё]{4,}", value):
        return True
    return False


def name_from_product_url(url: str | None) -> str:
    """Fallback title from /product/slug-words-123456/ when card text is only badges."""
    if not url:
        return ""
    match = re.search(
        r"/product/([^/?#]+?)-(\d{6,})(?:[/?#]|$)",
        str(url),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    slug = match.group(1).replace("-", " ").strip()
    slug = re.sub(r"\s+", " ", slug)
    # Skip empty / numeric-only / tiny slugs.
    if len(slug) < 8:
        return ""
    if not re.search(r"[A-Za-zА-Яа-яЁё]{3,}", slug):
        return ""
    return clean_product_name(slug)


def clean_product_name(text: str | None) -> str:
    """Strip listing badges glued into a title (e.g. «… 1 шт распродажа»)."""
    if not text:
        return ""
    value = str(text).replace("\xa0", " ").replace("\u2009", " ")
    value = re.sub(r"<[^>]+>", " ", value)
    value = " ".join(value.split()).strip(" ·•|-–—")
    if not value:
        return ""
    # Repeated passes remove stacked badges at the edges.
    for _ in range(4):
        cleaned = _PRODUCT_NAME_NOISE_INLINE.sub(" ", value)
        cleaned = " ".join(cleaned.split()).strip(" ·•|-–—")
        if cleaned == value:
            break
        value = cleaned
    if is_product_name_noise(value):
        return ""
    return value


def pick_product_name(*sources: str | None) -> str:
    """Choose the best product title from card text / HTML / link labels."""
    candidates: list[str] = []
    seen: set[str] = set()

    def consider(raw: str | None) -> None:
        if not raw:
            return
        for part in re.split(r"[\n\r\t|]+", str(raw)):
            cleaned = clean_product_name(part)
            if not cleaned or cleaned.lower() in seen:
                continue
            seen.add(cleaned.lower())
            candidates.append(cleaned)
        # Also try the whole blob after cleaning (multi-word titles).
        whole = clean_product_name(raw)
        if whole and whole.lower() not in seen:
            seen.add(whole.lower())
            candidates.append(whole)

    for source in sources:
        consider(source)

    if not candidates:
        return ""

    def score(name: str) -> tuple[int, int, int, int]:
        words = len(name.split())
        letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", name))
        lowered = name.lower()
        # Heavy penalty for leftover badge phrases that slipped past filters.
        badge_penalty = 0
        if "бренд" in lowered or "распродаж" in lowered or "хит" == lowered:
            badge_penalty = -1000
        # Prefer real titles: longer, more words/letters, not badge-like.
        return (badge_penalty, letters, words, len(name))

    return max(candidates, key=score)


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
