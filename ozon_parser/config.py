from pathlib import Path
from typing import Literal

BrowserMode = Literal["desktop", "mobile"]

DESKTOP_MODE: BrowserMode = "desktop"
MOBILE_MODE: BrowserMode = "mobile"

BASE_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = BASE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
SESSION_DIR = BASE_DIR / "session"
OUTPUT_DIR = Path(r"C:\Ozon")
CHROME_OZON_PROFILE = Path(r"C:\Ozon\ChromeProfile")
MOBILE_CHROME_PROFILE = SESSION_DIR / "mobile_browser_profile"

MOBILE_BASE_URL = "https://m.ozon.ru"
DESKTOP_BASE_URL = "https://www.ozon.ru"

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

# Human-like delays (seconds)
DELAY_PAGE_MIN = 3.0
DELAY_PAGE_MAX = 6.0
DELAY_SCROLL_MIN = 1.5
DELAY_SCROLL_MAX = 3.5
DELAY_CLICK_MIN = 0.5
DELAY_CLICK_MAX = 1.5
DELAY_BETWEEN_CATEGORIES_MIN = 8.0
DELAY_BETWEEN_CATEGORIES_MAX = 15.0
DELAY_BETWEEN_PRODUCT_PAGES_MIN = 12.0
DELAY_BETWEEN_PRODUCT_PAGES_MAX = 20.0

MAX_BLOCK_RECOVERY_ATTEMPTS = 5
MAX_CATEGORY_RETRIES = 2
MAX_PRODUCT_DETAIL_FETCHES = 15
# Полный рекурсивный обход открывает каждую категорию отдельно.
CATALOG_LOAD_TIMEOUT_SEC = 900

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
)
