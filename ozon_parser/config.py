from pathlib import Path
from typing import Literal

from .paths import resource_root, writable_root

BrowserMode = Literal["desktop", "mobile"]

DESKTOP_MODE: BrowserMode = "desktop"
MOBILE_MODE: BrowserMode = "mobile"

BASE_DIR = writable_root()
ASSETS_DIR = resource_root() / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
OUTPUT_DIR = Path(r"C:\Ozon")
SESSION_DIR = OUTPUT_DIR / "session"
CHROME_OZON_PROFILE = OUTPUT_DIR / "ChromeProfile"
MOBILE_CHROME_PROFILE = SESSION_DIR / "mobile_browser_profile"

MOBILE_BASE_URL = "https://m.ozon.ru"
MOBILE_WARMUP_URL = MOBILE_BASE_URL + "/"
# Guest mobile sessions that warm up on www.ozon.ru keep desktop host for catalog URLs.
MOBILE_DESKTOP_HOST_SESSIONS = frozenset({
    "mobile_guest_cdp",
    "mobile_guest_www",
})
DESKTOP_BASE_URL = "https://www.ozon.ru"
ALL_SELLERS_PATH = "/seller/"
GLOBAL_CATALOG_PATH = "/category/"

CHROME_DEBUG_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CHROME_DEBUG_PORT}"

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36"
)

DESKTOP_VIEWPORT = {"width": 1366, "height": 768}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
MOBILE_DEVICE_SCALE_FACTOR = 2.75

# Human-like delays (seconds). Tuned for long runs with lower ban risk.
DELAY_PAGE_MIN = 4.0
DELAY_PAGE_MAX = 8.0
DELAY_SCROLL_MIN = 2.5
DELAY_SCROLL_MAX = 5.0
DELAY_CLICK_MIN = 0.8
DELAY_CLICK_MAX = 2.0
DELAY_BETWEEN_CATEGORIES_MIN = 25.0
DELAY_BETWEEN_CATEGORIES_MAX = 45.0
DELAY_BETWEEN_PRODUCT_PAGES_MIN = 12.0
DELAY_BETWEEN_PRODUCT_PAGES_MAX = 20.0

# Soft session budgets. Large goals are collected across multiple short sessions
# with checkpoint resume — this is the safest legitimate pattern for Ozon.
SESSION_MAX_CATEGORIES = 4
SESSION_MAX_PRODUCTS = 300
# Global "all sellers" mode triggers fab_ faster; keep sessions tiny.
GLOBAL_SESSION_MAX_CATEGORIES = 1
GLOBAL_SESSION_MAX_PRODUCTS = 80
GLOBAL_LARGE_SELECTION_THRESHOLD = 50
CHECKPOINT_SAVE_EVERY_PRODUCTS = 20

SAFE_CATEGORY_BATCH_SIZE = 3
SAFE_CATEGORY_BREAK_MIN = 120.0
SAFE_CATEGORY_BREAK_MAX = 240.0
GLOBAL_SAFE_CATEGORY_BATCH_SIZE = 1
GLOBAL_SAFE_CATEGORY_BREAK_MIN = 180.0
GLOBAL_SAFE_CATEGORY_BREAK_MAX = 300.0
GLOBAL_FIRST_CATEGORY_PAUSE_MIN = 20.0
GLOBAL_FIRST_CATEGORY_PAUSE_MAX = 35.0
GLOBAL_BETWEEN_CATEGORIES_MIN = 40.0
GLOBAL_BETWEEN_CATEGORIES_MAX = 70.0
SAFE_SCROLL_BATCH_SIZE = 8
SAFE_SCROLL_BREAK_MIN = 45.0
SAFE_SCROLL_BREAK_MAX = 90.0
BLOCK_COOLDOWN_MIN = 45.0
BLOCK_COOLDOWN_MAX = 90.0
ANTIBOT_WAIT_TIMEOUT_SEC = 120.0

MAX_CATEGORY_RETRIES = 1
# Never auto-retry page.goto while an incident/fab_ page is active.
SAFE_GOTO_MAX_RETRIES = 1
# Detail-page opens are the main ban trigger. Bulk mode uses listing cards only.
MAX_PRODUCT_DETAIL_FETCHES = 0
CATALOG_LOAD_TIMEOUT_SEC = 900
# Smaller Composer batches for global tree load reduce antibot pressure.
GLOBAL_COMPOSER_BATCH_SIZE = 4

BONUS_PATTERNS = (
    "балл",
    "баллов",
    "за отзыв",
    "бонус",
)

BLOCK_MARKERS = (
    "похоже, нет соединения",
    "выключите vpn",
    "перезагрузите роутер",
)

CAPTCHA_MARKERS = (
    "подтвердите, что вы не робот",
    "captcha",
    "cf-challenge",
    "antibot challenge page",
    "abt-challenge",
)
