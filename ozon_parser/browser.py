"""Browser launch, session validation and navigation helpers."""

import re
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

from .auth import (
    browser_profile_dir,
    clear_mobile_session_marker,
    has_mobile_saved_session,
    has_persistent_profile,
    mobile_browser_profile_dir,
)
from .chrome_launcher import ensure_chrome_for_ozon
from .config import (
    CAPTCHA_MARKERS,
    CDP_URL,
    CHROME_OZON_PROFILE,
    DESKTOP_MODE,
    DESKTOP_USER_AGENT,
    DESKTOP_VIEWPORT,
    MAX_BLOCK_RECOVERY_ATTEMPTS,
    MOBILE_DEVICE_SCALE_FACTOR,
    MOBILE_MODE,
    MOBILE_USER_AGENT,
    MOBILE_VIEWPORT,
    BrowserMode,
)
from .utils import human_delay

SessionMode = str

DESKTOP_CONTEXT = {
    "user_agent": DESKTOP_USER_AGENT,
    "viewport": DESKTOP_VIEWPORT,
    "locale": "ru-RU",
    "timezone_id": "Europe/Moscow",
    "extra_http_headers": {"Accept-Language": "ru-RU,ru;q=0.9"},
}

MOBILE_CONTEXT = {
    "user_agent": MOBILE_USER_AGENT,
    "viewport": MOBILE_VIEWPORT,
    "device_scale_factor": MOBILE_DEVICE_SCALE_FACTOR,
    "is_mobile": True,
    "has_touch": True,
    "locale": "ru-RU",
    "timezone_id": "Europe/Moscow",
    "extra_http_headers": {"Accept-Language": "ru-RU,ru;q=0.9"},
}

BROWSER_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
]

MOBILE_AUTH_TOKEN_NAMES = {
    "__secure-access-token",
    "__secure-refresh-token",
    "access_token",
    "refresh_token",
}
MOBILE_USER_ID_COOKIE_NAMES = {
    "__secure-user-id",
    "user_id",
}
MOBILE_ACCOUNT_PATHS = (
    "/my/",
    "/my/main",
    "/my/account",
    "/profile",
)
MOBILE_ACCOUNT_URL = "https://m.ozon.ru/my/main"


def browser_context_options(browser_mode: BrowserMode = DESKTOP_MODE) -> dict:
    """Return a fresh Playwright context configuration for the selected mode."""
    if browser_mode == DESKTOP_MODE:
        return dict(DESKTOP_CONTEXT)
    if browser_mode == MOBILE_MODE:
        return dict(MOBILE_CONTEXT)
    raise ValueError(f"Неизвестный режим браузера: {browser_mode}")


def connect_via_cdp(playwright: Playwright, progress=None) -> tuple[Browser, BrowserContext, Page]:
    log = progress or (lambda _m: None)
    if not ensure_chrome_for_ozon(log):
        raise RuntimeError("Запустите Chrome через «Открыть Chrome для Ozon» или chrome_for_ozon.bat")
    browser = playwright.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.pages[0] if context.pages else context.new_page()
    return browser, context, page


def create_persistent_context(
    playwright: Playwright,
    headless: bool = False,
    user_data_dir: Path | str | None = None,
    browser_mode: BrowserMode = DESKTOP_MODE,
) -> tuple[BrowserContext, Page]:
    default_profile = (
        mobile_browser_profile_dir()
        if browser_mode == MOBILE_MODE
        else browser_profile_dir()
    )
    profile = str(user_data_dir or default_profile)
    for channel in ("chrome", "msedge", None):
        try:
            kwargs = {
                "user_data_dir": profile,
                "headless": headless,
                "args": BROWSER_ARGS,
                **browser_context_options(browser_mode),
            }
            if channel:
                context = playwright.chromium.launch_persistent_context(channel=channel, **kwargs)
            else:
                context = playwright.chromium.launch_persistent_context(**kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            return context, page
        except Exception:
            continue
    raise RuntimeError("Не удалось запустить браузер с профилем.")


def create_browser_context(
    playwright: Playwright,
    headless: bool,
    storage_state: dict | None = None,
    browser_mode: BrowserMode = DESKTOP_MODE,
) -> tuple[Browser, BrowserContext, Page]:
    launch_kwargs = {
        "headless": headless,
        "args": BROWSER_ARGS,
    }
    browser = None
    for channel in ("chrome", "msedge", None):
        try:
            browser = (
                playwright.chromium.launch(channel=channel, **launch_kwargs)
                if channel
                else playwright.chromium.launch(**launch_kwargs)
            )
            break
        except Exception:
            continue
    if not browser:
        raise RuntimeError("Установите Google Chrome или Microsoft Edge.")

    context_kwargs = browser_context_options(browser_mode)
    if storage_state:
        context_kwargs["storage_state"] = storage_state
    context = browser.new_context(**context_kwargs)
    return browser, context, context.new_page()


def _cookie_is_live(cookie: dict, now: float) -> bool:
    expires = float(cookie.get("expires", -1) or -1)
    return bool(cookie.get("value")) and (expires < 0 or expires > now)


def _ozon_cookies(context: BrowserContext) -> list[dict]:
    try:
        cookies = context.cookies()
    except Exception:
        return []
    result = []
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lower()
        if domain.endswith("ozon.ru"):
            result.append(cookie)
    return result


def mobile_context_is_authenticated(context: BrowserContext) -> bool:
    """Return True only for a real logged-in user, not a guest token."""
    now = time.time()
    has_live_token = False
    has_real_user = False

    for cookie in _ozon_cookies(context):
        name = str(cookie.get("name", "")).lower()
        if not _cookie_is_live(cookie, now):
            continue
        if name in MOBILE_AUTH_TOKEN_NAMES:
            has_live_token = True
        if name in MOBILE_USER_ID_COOKIE_NAMES:
            user_id = str(cookie.get("value", "")).strip()
            if user_id and user_id not in {"0", "null", "none", "undefined"}:
                has_real_user = True

    return has_live_token and has_real_user


def page_indicates_authenticated(page: Page) -> bool:
    """Best-effort UI/URL signals that the user finished login."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if any(path in url for path in MOBILE_ACCOUNT_PATHS):
        return True

    try:
        text = (page.evaluate("() => document.body?.innerText || ''") or "").lower()
    except Exception:
        return False

    positive = (
        "выйти",
        "мои заказы",
        "личный кабинет",
    )
    negative = (
        "войти или зарегистрироваться",
        "вход или регистрация",
        "подтвердите, что вы не робот",
    )
    if any(marker in text for marker in negative):
        return False
    return any(marker in text for marker in positive)


def verify_mobile_authentication(context: BrowserContext, page: Page | None = None) -> bool:
    if mobile_context_is_authenticated(context):
        return True
    if page is None:
        return False

    now = time.time()
    has_live_token = False
    for cookie in _ozon_cookies(context):
        name = str(cookie.get("name", "")).lower()
        if name in MOBILE_AUTH_TOKEN_NAMES and _cookie_is_live(cookie, now):
            has_live_token = True
            break

    # UI can confirm login a moment before user-id cookie settles.
    return has_live_token and page_indicates_authenticated(page)


def settle_mobile_login(page: Page, progress=None) -> None:
    """Open the account area so Ozon can finish writing login cookies."""
    log = progress or (lambda _m: None)
    try:
        current = (page.url or "").lower()
    except Exception:
        current = ""
    if any(path in current for path in MOBILE_ACCOUNT_PATHS):
        return
    try:
        log("Открываем личный кабинет для проверки входа...")
        page.goto(MOBILE_ACCOUNT_URL, wait_until="domcontentloaded", timeout=45000)
        human_delay(1.5, 2.5)
    except Exception:
        pass


def has_ozon_chrome_profile() -> bool:
    if not CHROME_OZON_PROFILE.exists():
        return False
    for name in ("Default", "Local State"):
        if (CHROME_OZON_PROFILE / name).exists():
            return True
    return False


def open_session_context(
    playwright: Playwright,
    headless: bool,
    storage_state: dict | None = None,
    use_cdp: bool = True,
    progress=None,
    prefer_ozon_profile: bool = True,
    browser_mode: BrowserMode = DESKTOP_MODE,
    use_auth: bool = False,
) -> tuple[Browser | None, BrowserContext, Page, SessionMode]:
    log = progress or (lambda _m: None)

    if browser_mode == MOBILE_MODE:
        if use_auth:
            if not has_mobile_saved_session():
                raise RuntimeError(
                    "Мобильная сессия не найдена. "
                    "Сначала нажмите «Войти в мобильный Ozon» и завершите вход."
                )
            context, page = create_persistent_context(
                playwright,
                headless=headless,
                user_data_dir=mobile_browser_profile_dir(),
                browser_mode=MOBILE_MODE,
            )
            if not mobile_context_is_authenticated(context):
                close_session_context(None, context, "mobile_persistent")
                clear_mobile_session_marker()
                raise RuntimeError(
                    "Сохранённая мобильная сессия истекла или вход не подтверждён. "
                    "Нажмите «Войти в мобильный Ozon» и войдите повторно."
                )
            log("Используем отдельный авторизованный мобильный профиль")
            return None, context, page, "mobile_persistent"

        browser, context, page = create_browser_context(
            playwright,
            headless,
            storage_state=None,
            browser_mode=MOBILE_MODE,
        )
        log("Используем чистую гостевую мобильную сессию")
        return browser, context, page, "mobile_guest"

    if use_cdp:
        try:
            if not ensure_chrome_for_ozon(log):
                raise RuntimeError("Chrome для Ozon не запущен")
            browser, context, page = connect_via_cdp(playwright, log)
            return browser, context, page, "cdp"
        except Exception as exc:
            log(f"CDP недоступен ({exc}), пробуем другой режим...")

    if storage_state and storage_state.get("cookies"):
        try:
            browser, context, page = create_browser_context(playwright, headless, storage_state)
            return browser, context, page, "standard"
        except Exception as exc:
            log(f"Сессия с cookies: {exc}")

    if prefer_ozon_profile and has_ozon_chrome_profile():
        try:
            context, page = create_persistent_context(
                playwright, headless=headless, user_data_dir=CHROME_OZON_PROFILE,
            )
            log("Используем профиль Chrome для Ozon")
            return None, context, page, "ozon_persistent"
        except Exception as exc:
            log(f"Профиль Chrome Ozon: {exc}")

    if has_persistent_profile():
        try:
            context, page = create_persistent_context(playwright, headless=headless)
            return None, context, page, "persistent"
        except Exception:
            pass

    browser, context, page = create_browser_context(playwright, headless, storage_state)
    return browser, context, page, "standard"


def close_session_context(browser: Browser | None, context: BrowserContext, mode: SessionMode) -> None:
    if mode == "cdp":
        return
    try:
        context.close()
    except Exception:
        pass
    if browser:
        try:
            browser.close()
        except Exception:
            pass


def extract_incident_id(page: Page) -> str | None:
    try:
        text = page.evaluate("() => document.body?.innerText || ''") or ""
        match = re.search(r"Инцидент:\s*(\S+)", text, re.IGNORECASE)
        return match.group(1) if match else None
    except Exception:
        return None


def is_blocked_page(page: Page) -> bool:
    try:
        text = (page.evaluate("() => document.body?.innerText || ''") or "").lower()
        if "похоже, нет соединения" in text:
            return True
        if "выключите vpn" in text and "перезагрузите роутер" in text:
            return True
        if "инцидент:" in text and "fab_chlg" in text:
            return True
        return False
    except Exception:
        return False


def is_captcha_page(page: Page) -> bool:
    try:
        text = (page.evaluate("() => document.body?.innerText || ''") or "").lower()
        return any(marker in text for marker in CAPTCHA_MARKERS)
    except Exception:
        return False


def is_seller_page(page: Page) -> bool:
    return "/seller/" in page.url.lower()


def recover_access(
    page: Page,
    progress=None,
    on_manual_bypass: Callable[[str | None], bool | None] | None = None,
    target_url: str | None = None,
) -> bool:
    log = progress or (lambda _m: None)
    for attempt in range(1, MAX_BLOCK_RECOVERY_ATTEMPTS + 1):
        if not is_blocked_page(page) and not is_captcha_page(page):
            return True

        incident = extract_incident_id(page)
        log(f"Блокировка — восстановление ({attempt}/{MAX_BLOCK_RECOVERY_ATTEMPTS})")
        if incident:
            log(f"Инцидент: {incident}")

        if on_manual_bypass:
            if on_manual_bypass(incident) is False:
                log("Ручное подтверждение не получено; повтор отменён")
                return False

        human_delay(3.0, 5.0)

        try:
            if target_url:
                page.goto(target_url, wait_until="domcontentloaded", timeout=90000)
            else:
                page.reload(wait_until="domcontentloaded", timeout=90000)
            human_delay(3.0, 5.0)
        except Exception:
            pass

        if not is_blocked_page(page) and not is_captcha_page(page):
            log("Доступ восстановлен")
            return True
    return False


def _normalize_url(url: str) -> str:
    return url.split("#")[0].strip().rstrip("/")


def safe_goto(
    page: Page,
    url: str,
    progress=None,
    max_retries: int = 3,
    on_manual_bypass: Callable[[str | None], bool | None] | None = None,
) -> bool:
    log = progress or (lambda _m: None)
    target = _normalize_url(url)
    for attempt in range(1, max_retries + 1):
        try:
            current = _normalize_url(page.url)
            if current != target:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
            human_delay(2.0, 4.0)

            if is_blocked_page(page) or is_captcha_page(page):
                if recover_access(page, log, on_manual_bypass, target_url=url):
                    return True
                if attempt < max_retries:
                    continue
                return False
            return True
        except Exception as exc:
            log(f"Ошибка загрузки: {exc}")
            if attempt < max_retries:
                human_delay(3.0, 5.0)
            else:
                return False
    return False
