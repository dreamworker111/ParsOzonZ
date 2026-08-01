"""Configuration for Ozon category collector."""

from __future__ import annotations

from pathlib import Path

from ozon_parser.paths import is_frozen, writable_root

if is_frozen():
    BASE_DIR = Path(r"C:\Ozon")
else:
    BASE_DIR = writable_root()

DEFAULT_CACHE_DIR = BASE_DIR / "cache" / "ozon_categories"
DEFAULT_OUTPUT_DIR = BASE_DIR / "data" / "ozon_categories"

DESKTOP_BASE_URL = "https://www.ozon.ru"
COMPOSER_API_PATH = "/api/composer-api.bx/page/json/v2"

# Primary source: Ozon Composer API (same endpoint as dweb client).
COMPOSER_APP_NAME = "dweb_client"

MAX_RETRIES = 5
RETRY_BASE_DELAY_SEC = 1.0
RETRY_MAX_DELAY_SEC = 60.0

REQUEST_TIMEOUT_SEC = 45.0
MIN_REQUEST_INTERVAL_SEC = 0.35

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

# HTML fallback: category filter block class on seller/catalog pages.
CATEGORY_BLOCK_CLASS = "wb6_7"

CACHE_FILENAME = "categories_cache.json"
FINGERPRINT_FILENAME = "structure_fingerprint.txt"
