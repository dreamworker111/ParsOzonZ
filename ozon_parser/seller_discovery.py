"""Discover Ozon seller storefront URLs from the marketplace directory."""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse

from playwright.sync_api import Page

from .browser import safe_goto
from .config import ALL_SELLERS_PATH, SAFE_GOTO_MAX_RETRIES
from .utils import human_scroll_delay, to_desktop_url

_COLLECT_SELLERS_SCRIPT = """
() => {
    const urls = new Set();
    const skip = new Set(['/seller', '/seller/0', '/seller/0/']);
    document.querySelectorAll('a[href*="/seller/"]').forEach((link) => {
        const raw = link.href || link.getAttribute('href') || '';
        if (!raw) return;
        try {
            const u = new URL(raw, window.location.origin);
            if (!u.hostname.includes('ozon.ru')) return;
            let path = u.pathname || '';
            if (!path.endsWith('/')) path += '/';
            if (skip.has(path.replace(/\\/$/, '')) || skip.has(path)) return;
            if (!/^\\/seller\\/[^/]+\\/$/i.test(path)) return;
            urls.add(u.origin + path);
        } catch (e) {}
    });
    return Array.from(urls);
}
"""


def _normalize_seller_url(url: str) -> str:
    parsed = urlparse(to_desktop_url(url.strip()))
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def discover_marketplace_sellers(
    page: Page,
    *,
    base_url: str = "https://www.ozon.ru",
    progress: Callable[[str], None] | None = None,
    on_manual_bypass=None,
    max_sellers: int = 300,
    scroll_rounds: int = 25,
) -> list[str]:
    """Collect seller storefront URLs from Ozon's seller directory page."""
    log = progress or (lambda _msg: None)
    listing_url = to_desktop_url(base_url.rstrip("/") + ALL_SELLERS_PATH)
    if not safe_goto(
        page,
        listing_url,
        log,
        max_retries=SAFE_GOTO_MAX_RETRIES,
        on_manual_bypass=on_manual_bypass,
    ):
        log("Не удалось открыть список магазинов Ozon")
        return []

    seen: set[str] = set()
    stagnant = 0
    for round_idx in range(scroll_rounds):
        try:
            batch = page.evaluate(_COLLECT_SELLERS_SCRIPT) or []
        except Exception:
            batch = []
        before = len(seen)
        for href in batch:
            if not isinstance(href, str):
                continue
            seen.add(_normalize_seller_url(href))
        if len(seen) >= max_sellers:
            break
        if len(seen) == before:
            stagnant += 1
            if stagnant >= 4:
                break
        else:
            stagnant = 0
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        human_scroll_delay()
        if round_idx and round_idx % 5 == 0:
            log(f"Список магазинов: найдено {len(seen)}…")

    sellers = sorted(seen)[:max_sellers]
    log(f"Найдено магазинов для обхода: {len(sellers)}")
    return sellers
