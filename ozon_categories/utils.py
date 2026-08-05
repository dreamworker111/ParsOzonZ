"""Utilities: URLs, validation, rate limit, retries, user-agent rotation."""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from .config import (
    MAX_RETRIES,
    MIN_REQUEST_INTERVAL_SEC,
    RETRY_BASE_DELAY_SEC,
    RETRY_MAX_DELAY_SEC,
    USER_AGENTS,
)

P = ParamSpec("P")
R = TypeVar("R")

logger = logging.getLogger(__name__)

SKIP_NAMES = frozenset({
    "ещё",
    "eще",
    "еще",
    "все",
    "all",
    "показать все",
    "посмотреть все",
    "смотреть все",
    "показать ещё",
    "показать еще",
    "свернуть",
    "развернуть",
})
INVALID_NAME_RE = re.compile(
    r"(^|[.\s])(profile|widget|web[A-Z]|myProfile|layout|state|config|token|session)([.\s]|$)",
    re.I,
)
CAMELCASE_KEY_RE = re.compile(r"^[a-z]+[A-Z][a-zA-Z0-9]*$")
DOT_KEY_RE = re.compile(r"^[a-zA-Z_]+\.[a-zA-Z_.]+$")


def is_valid_category_id(category_id: str) -> bool:
    return bool(re.fullmatch(r"\d{2,}", str(category_id).strip()))


def is_valid_category_name(name: str) -> bool:
    name = str(name).strip()
    if not name or len(name) < 2 or len(name) > 120:
        return False
    if name.lower() in SKIP_NAMES:
        return False
    if not re.search(r"[а-яА-ЯёЁa-zA-Z0-9]", name):
        return False
    if DOT_KEY_RE.match(name):
        return False
    if CAMELCASE_KEY_RE.match(name):
        return False
    if INVALID_NAME_RE.search(name):
        return False
    if "." in name and not re.search(r"[а-яА-ЯёЁ]", name):
        return False
    return True


def is_valid_category(name: str, category_id: str) -> bool:
    return is_valid_category_id(category_id) and is_valid_category_name(name)


def to_desktop_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if not url.startswith("http"):
        url = f"https://{url}"
    parsed = urlparse(url)
    host = parsed.netloc.replace("m.ozon.ru", "www.ozon.ru")
    if "ozon.ru" not in host:
        host = "www.ozon.ru"
    return urlunparse((parsed.scheme or "https", host, parsed.path, parsed.params, parsed.query, parsed.fragment))


def normalize_source_url(url: str) -> str:
    desktop = to_desktop_url(url)
    parsed = urlparse(desktop)
    if not parsed.path or parsed.path == "/":
        raise ValueError("source_url must point to Ozon seller or category page")
    return desktop


def seller_base_path(source_url: str) -> str:
    parsed = urlparse(normalize_source_url(source_url))
    path = parsed.path
    if not path.endswith("/"):
        path += "/"
    return path


def build_category_path(source_url: str, category_id: str | None = None) -> str:
    parsed = urlparse(normalize_source_url(source_url))
    path = parsed.path
    if not path.endswith("/"):
        path += "/"
    query = urlencode({"category": category_id}) if category_id else ""
    return f"{path}?{query}" if query else path


def build_category_url(source_url: str, category_id: str | None = None) -> str:
    parsed = urlparse(normalize_source_url(source_url))
    path = build_category_path(source_url, category_id)
    return urlunparse((parsed.scheme or "https", parsed.netloc, path.split("?")[0], "", path.split("?")[1] if "?" in path else "", ""))


def absolute_ozon_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    return to_desktop_url(urljoin(base_url, href))


def category_id_from_url(url: str) -> str | None:
    qs = parse_qs(urlparse(url).query)
    vals = qs.get("category") or qs.get("Category") or []
    return str(vals[0]) if vals else None


class UserAgentRotator:
    """Round-robin / random User-Agent rotation."""

    def __init__(self, agents: tuple[str, ...] = USER_AGENTS) -> None:
        self._agents = agents
        self._index = 0

    def next(self) -> str:
        if len(self._agents) == 1:
            return self._agents[0]
        if random.random() < 0.5:
            return random.choice(self._agents)
        agent = self._agents[self._index % len(self._agents)]
        self._index += 1
        return agent


class RateLimiter:
    """Simple minimum-interval rate limiter."""

    def __init__(self, min_interval_sec: float = MIN_REQUEST_INTERVAL_SEC) -> None:
        self._min_interval = min_interval_sec
        self._last_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_at = time.monotonic()


def retry_with_backoff(
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY_SEC,
    max_delay: float = RETRY_MAX_DELAY_SEC,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator: exponential backoff retries."""

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    attempt += 1
                    if attempt > max_retries:
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay *= random.uniform(0.85, 1.15)
                    logger.warning(
                        "Retry %s/%s after error: %s (sleep %.2fs)",
                        attempt,
                        max_retries,
                        exc,
                        delay,
                    )
                    time.sleep(delay)

        return wrapper

    return decorator
