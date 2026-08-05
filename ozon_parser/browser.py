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
    mobile_guest_profile_dir,
)
from .chrome_launcher import ensure_chrome_for_ozon
from .config import (
    ANTIBOT_WAIT_TIMEOUT_SEC,
    BLOCK_COOLDOWN_MAX,
    BLOCK_COOLDOWN_MIN,
    BLOCK_MARKERS,
    CAPTCHA_MARKERS,
    CDP_URL,
    CHROME_OZON_PROFILE,
    DESKTOP_BASE_URL,
    DESKTOP_MODE,
    DESKTOP_USER_AGENT,
    DESKTOP_VIEWPORT,
    MOBILE_DEVICE_SCALE_FACTOR,
    MOBILE_MODE,
    MOBILE_USER_AGENT,
    MOBILE_VIEWPORT,
    MOBILE_WARMUP_URL,
    SAFE_GOTO_MAX_RETRIES,
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

MOBILE_STEALTH_INIT = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = window.chrome || { runtime: {} };
}
"""

PERSISTENT_SESSION_MODES = frozenset({
    "cdp",
    "ozon_persistent",
    "mobile_persistent",
    "mobile_guest",
    "mobile_guest_chrome",
    "mobile_guest_cdp",
    "mobile_guest_www",
})


def session_uses_desktop_host(mode: SessionMode) -> bool:
    from .config import MOBILE_DESKTOP_HOST_SESSIONS

    return mode in MOBILE_DESKTOP_HOST_SESSIONS


def browser_context_options(browser_mode: BrowserMode = DESKTOP_MODE) -> dict:
    """Return a fresh Playwright context configuration for the selected mode."""
    if browser_mode == DESKTOP_MODE:
        return dict(DESKTOP_CONTEXT)
    if browser_mode == MOBILE_MODE:
        return dict(MOBILE_CONTEXT)
    raise ValueError(f"Неизвестный режим браузера: {browser_mode}")


def _apply_mobile_stealth(context: BrowserContext) -> None:
    try:
        context.add_init_script(MOBILE_STEALTH_INIT)
    except Exception:
        pass


def connect_via_cdp(playwright: Playwright, progress=None) -> tuple[Browser, BrowserContext, Page]:
    log = progress or (lambda _m: None)
    if not ensure_chrome_for_ozon(log):
        raise RuntimeError(
            "Не удалось подключить Chrome. Нажмите «Загрузить категории» — "
            "Chrome откроется автоматически, либо запустите chrome_for_ozon.bat"
        )
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
            if browser_mode == MOBILE_MODE:
                _apply_mobile_stealth(context)
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
    if browser_mode == MOBILE_MODE:
        _apply_mobile_stealth(context)
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


def page_has_usable_ozon_content(page: Page) -> bool:
    """True when the tab shows a real Ozon page, not an empty antibot shell."""
    try:
        title = str(page.title() or "").lower()
    except Exception:
        title = ""
    if "antibot challenge page" in title:
        return False
    try:
        text = str(
            page.evaluate("() => (document.body?.innerText || '').replace(/\\s+/g, ' ').trim()")
            or ""
        )
    except Exception:
        text = ""
    if len(text) < 60:
        return False
    lowered = text.lower()
    if is_blocked_page(page):
        return False
    markers = (
        "ozon",
        "магазин",
        "категор",
        "товар",
        "корзин",
        "каталог",
        "продав",
        "электроник",
    )
    return any(marker in lowered or marker in title for marker in markers)


# «Сбросить фильтры» alone is NOT empty — it appears on normal listings with
# sorting/active chips. Require an actual empty-result phrase.
EMPTY_CATALOG_MARKERS = (
    "не нашли товары",
    "ничего не нашлось",
    "по вашим параметрам ничего не нашлось",
)


def is_empty_catalog_filter_page(page: Page) -> bool:
    """True when Ozon shows the empty catalog state (no products for filters)."""
    try:
        snapshot = page.evaluate(
            """() => {
                const text = (document.body?.innerText || '')
                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                const productLinks = document.querySelectorAll('a[href*="/product/"]').length;
                return { text, productLinks };
            }"""
        ) or {}
    except Exception:
        return False
    if not isinstance(snapshot, dict):
        return False
    text = str(snapshot.get("text") or "")
    if not text:
        return False
    # Real product tiles mean the listing is usable even if a reset chip is visible.
    try:
        product_links = int(snapshot.get("productLinks") or 0)
    except (TypeError, ValueError):
        product_links = 0
    if product_links >= 3:
        return False
    return any(marker in text for marker in EMPTY_CATALOG_MARKERS)


def try_reset_catalog_filters(page: Page) -> bool:
    """Click «Сбросить фильтры» when the empty catalog state is shown."""
    selectors = (
        'button:has-text("Сбросить фильтры")',
        'a:has-text("Сбросить фильтры")',
        '[role="button"]:has-text("Сбросить фильтры")',
        'span:has-text("Сбросить фильтры")',
        'div:has-text("Сбросить фильтры")',
    )
    for selector in selectors:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                el.click()
                time.sleep(1.2)
                return True
        except Exception:
            continue
    return False


def is_antibot_challenge_page(page: Page) -> bool:
    """Detect an active antibot challenge shell — not a finished page with leftover query flags."""
    try:
        title = str(page.title() or "").lower()
        if "antibot challenge page" in title:
            return True
        # After a passed check Ozon often keeps ?__rr=1&abt_att=1 on a normal page.
        # That must NOT be treated as a challenge, otherwise catalog load never starts.
        if page_has_usable_ozon_content(page):
            return False
        try:
            text = str(
                page.evaluate("() => (document.body?.innerText || '').trim()") or ""
            )
        except Exception:
            text = ""
        try:
            html = str(
                page.evaluate("() => document.documentElement?.innerHTML || ''") or ""
            ).lower()
        except Exception:
            html = ""
        if "abt-challenge" in html and len(text) < 80:
            return True
        url = str(getattr(page, "url", "") or "").lower()
        if "__rr=1" in url and "ozon.ru" in url and len(text) < 80:
            return True
        return False
    except Exception:
        return False


def is_access_restricted(page: Page) -> bool:
    """True when Ozon is blocked, showing captcha, or running an antibot challenge."""
    if is_blocked_page(page) or is_captcha_page(page):
        return True
    if is_antibot_challenge_page(page):
        return True
    return False


def wait_for_ozon_ready(
    page: Page,
    progress=None,
    timeout_sec: float | None = None,
) -> bool:
    """Wait until Ozon finishes its antibot challenge and shows a real page."""
    log = progress or (lambda _m: None)
    deadline = time.time() + (timeout_sec if timeout_sec is not None else ANTIBOT_WAIT_TIMEOUT_SEC)
    logged_wait = False

    while time.time() < deadline:
        try:
            if is_blocked_page(page) or is_captcha_page(page):
                return False
            if page_has_usable_ozon_content(page):
                return True
            if not is_antibot_challenge_page(page):
                try:
                    text = (
                        page.evaluate("() => document.body?.innerText || ''") or ""
                    ).strip()
                except Exception:
                    text = ""
                title = str(page.title() or "").lower()
                if text or ("ozon" in title and "antibot challenge page" not in title):
                    return True
        except Exception as exc:
            # Tab closed mid-wait — treat as not ready.
            if "closed" in str(exc).lower():
                return False
        if not logged_wait:
            log("Ozon проверяет браузер, ожидаем загрузку...")
            logged_wait = True
        human_delay(3.0, 5.0)

    try:
        return page_has_usable_ozon_content(page) or (
            not is_antibot_challenge_page(page) and not is_blocked_page(page)
        )
    except Exception:
        return False


def ensure_ozon_session_ready(
    page: Page,
    progress=None,
    warmup_url: str | None = None,
    on_manual_bypass: Callable[[str | None], bool | None] | None = None,
) -> bool:
    """Warm up the current browser tab and wait out Ozon antibot checks."""
    log = progress or (lambda _m: None)
    try:
        current = (page.url or "").lower()
    except Exception:
        current = ""

    target = warmup_url or (DESKTOP_BASE_URL.rstrip("/") + "/")

    # Never issue another navigation while an incident/fab_ page is already open.
    # Extra reloads create new incident IDs and prolong the IP block.
    if is_blocked_page(page):
        incident = extract_incident_id(page)
        log(
            "Ozon уже показал блокировку"
            + (f" ({incident})" if incident else "")
            + ". Новые запросы не отправляем — дождитесь 15–30 минут в Chrome."
        )
        return recover_access(page, log, on_manual_bypass, target_url=target)

    if "ozon.ru" not in current:
        log("Подготовка сессии Ozon...")
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=90000)
            human_delay(2.0, 4.0)
        except Exception as exc:
            log(f"Прогрев Ozon: {exc}")
    elif is_antibot_challenge_page(page):
        log("Ozon проверяет браузер, ожидаем без перезагрузки...")

    if wait_for_ozon_ready(page, log):
        return True

    if is_blocked_page(page):
        incident = extract_incident_id(page)
        if incident:
            log(f"Инцидент после прогрева: {incident}")
        return recover_access(page, log, on_manual_bypass, target_url=target)

    if is_antibot_challenge_page(page):
        log("Ozon проверяет браузер — подтвердите в Chrome и нажмите «Продолжить»")
        if recover_access(page, log, on_manual_bypass, target_url=target):
            return wait_for_ozon_ready(page, log)
        return False

    if is_captcha_page(page):
        return recover_access(page, log, on_manual_bypass, target_url=target)

    return False


def prepare_mobile_guest_session(page: Page, progress=None) -> bool:
    """Warm up a mobile guest session before catalog requests."""
    log = progress or (lambda _m: None)
    try:
        current = (page.url or "").lower()
    except Exception:
        current = ""

    if "ozon.ru" not in current or is_blocked_page(page) or is_captcha_page(page):
        log("Прогрев мобильной сессии Ozon...")
        try:
            page.goto(
                DESKTOP_BASE_URL.rstrip("/") + "/",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            human_delay(2.0, 4.0)
        except Exception as exc:
            log(f"Прогрев desktop Ozon: {exc}")

        if not is_blocked_page(page) and not is_captcha_page(page):
            try:
                page.goto(MOBILE_WARMUP_URL, wait_until="domcontentloaded", timeout=90000)
                human_delay(2.0, 4.0)
            except Exception as exc:
                log(f"Прогрев m.ozon.ru: {exc}")

    if not wait_for_ozon_ready(page, log):
        if is_antibot_challenge_page(page):
            log("Ozon не завершил проверку браузера за отведённое время")
        elif is_blocked_page(page) or is_captcha_page(page):
            incident = extract_incident_id(page)
            if incident:
                log(f"Мобильный Ozon заблокирован. Инцидент: {incident}")
            else:
                log("Мобильный Ozon заблокирован.")
        else:
            log("Мобильный Ozon не ответил за отведённое время")
        if not is_blocked_page(page) and not is_captcha_page(page):
            return False
        log("Пробуем www.ozon.ru для мобильного гостевого режима...")
        try:
            page.goto(
                DESKTOP_BASE_URL.rstrip("/") + "/",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            human_delay(2.0, 4.0)
        except Exception as exc:
            log(f"Переход на www.ozon.ru: {exc}")
        if not wait_for_ozon_ready(page, log):
            return False

    if is_blocked_page(page) or is_captcha_page(page):
        incident = extract_incident_id(page)
        if incident:
            log(f"Мобильный Ozon заблокирован. Инцидент: {incident}")
        else:
            log("Мобильный Ozon заблокирован.")
        return False

    log("Мобильная сессия готова")
    return True


def _apply_mobile_cdp_emulation(page: Page) -> None:
    try:
        session = page.context.new_cdp_session(page)
        session.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": MOBILE_VIEWPORT["width"],
                "height": MOBILE_VIEWPORT["height"],
                "deviceScaleFactor": MOBILE_DEVICE_SCALE_FACTOR,
                "mobile": True,
            },
        )
        session.send(
            "Emulation.setUserAgentOverride",
            {"userAgent": MOBILE_USER_AGENT},
        )
        session.send(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": True},
        )
    except Exception:
        pass


def open_mobile_guest_via_cdp(
    playwright: Playwright,
    log: Callable[[str], None],
) -> tuple[Browser, BrowserContext, Page] | None:
    """Reuse the live Chrome-for-Ozon tab with mobile emulation on www.ozon.ru."""
    if not ensure_chrome_for_ozon(log):
        return None
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
    except Exception as exc:
        log(f"CDP для мобильного режима недоступен: {exc}")
        return None

    context = browser.contexts[0] if browser.contexts else None
    if not context:
        return None

    page = context.pages[0] if context.pages else context.new_page()
    _apply_mobile_cdp_emulation(page)

    try:
        current = (page.url or "").lower()
    except Exception:
        current = ""

    if (
        "ozon.ru" in current
        and "m.ozon.ru" not in current
        and not is_blocked_page(page)
        and not is_captcha_page(page)
        and not is_antibot_challenge_page(page)
        and wait_for_ozon_ready(page, log, timeout_sec=30)
    ):
        log("Используем уже открытую вкладку Chrome для Ozon")
        return browser, context, page

    try:
        page.goto(
            DESKTOP_BASE_URL.rstrip("/") + "/",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        human_delay(2.0, 4.0)
    except Exception as exc:
        log(f"Переход на www.ozon.ru: {exc}")
        return None

    if wait_for_ozon_ready(page, log) and not is_blocked_page(page) and not is_captcha_page(page):
        return browser, context, page
    return None


def open_mobile_guest_session(
    playwright: Playwright,
    headless: bool,
    log: Callable[[str], None],
) -> tuple[Browser | None, BrowserContext, Page, SessionMode]:
    """Open a mobile session without authorization, preferring warmed profiles."""
    profile_errors: list[str] = []

    cdp_session = open_mobile_guest_via_cdp(playwright, log)
    if cdp_session:
        browser, context, page = cdp_session
        log("Мобильный режим через Chrome для Ozon (CDP + www)")
        return browser, context, page, "mobile_guest_cdp"

    if has_ozon_chrome_profile():
        try:
            context, page = create_persistent_context(
                playwright,
                headless=headless,
                user_data_dir=CHROME_OZON_PROFILE,
                browser_mode=MOBILE_MODE,
            )
            if prepare_mobile_guest_session(page, log):
                mode = "mobile_guest_www"
                try:
                    if "m.ozon.ru" in (page.url or "").lower():
                        mode = "mobile_guest_chrome"
                except Exception:
                    mode = "mobile_guest_chrome"
                log("Мобильный режим через профиль Chrome для Ozon")
                return None, context, page, mode
            close_session_context(None, context, "mobile_guest_chrome")
            profile_errors.append("профиль Chrome для Ozon заблокирован")
        except Exception as exc:
            profile_errors.append(f"профиль Chrome для Ozon: {exc}")

    try:
        context, page = create_persistent_context(
            playwright,
            headless=headless,
            user_data_dir=mobile_guest_profile_dir(),
            browser_mode=MOBILE_MODE,
        )
        if prepare_mobile_guest_session(page, log):
            mode = "mobile_guest_www"
            try:
                if "m.ozon.ru" in (page.url or "").lower():
                    mode = "mobile_guest"
            except Exception:
                mode = "mobile_guest"
            log("Используем сохранённый гостевой мобильный профиль")
            return None, context, page, mode
        close_session_context(None, context, "mobile_guest")
        profile_errors.append("гостевой мобильный профиль заблокирован")
    except Exception as exc:
        profile_errors.append(f"гостевой мобильный профиль: {exc}")

    details = "; ".join(profile_errors) if profile_errors else "неизвестная ошибка"
    raise RuntimeError(
        "Мобильный Ozon недоступен без авторизации. "
        f"{details}. Chrome откроется при «Загрузить категории»; дождитесь нормальной загрузки "
        "сайта без «Инцидента», подождите 15–30 минут и повторите."
    )


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

        return open_mobile_guest_session(playwright, headless, log)

    if use_cdp:
        try:
            if not ensure_chrome_for_ozon(log):
                raise RuntimeError("Chrome для Ozon не запущен")
            browser, context, page = connect_via_cdp(playwright, log)
            # Do not force-navigate here: run()/load_categories() ensure once
            # with a proper manual-bypass callback. Avoids discarded fab_ recovery.
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
    if mode in {"cdp", "mobile_guest_cdp"}:
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
        if match:
            return match.group(1)
        url = page.url or ""
        for source in (url, text):
            match = re.search(r"(fab_(?:chlg_)?[\w]+)", source, re.IGNORECASE)
            if match:
                return match.group(1)
        return None
    except Exception:
        return None


def is_blocked_page(page: Page) -> bool:
    try:
        text = str(
            page.evaluate("() => document.body?.innerText || ''") or ""
        ).lower()
        title = str(page.title() or "").lower()
        url = str(getattr(page, "url", "") or "").lower()
        # Ignore non-string mock/proxy values from unit tests.
        if title.startswith("<mock"):
            title = ""
        if url.startswith("<mock"):
            url = ""
        combined = f"{title}\n{text}"
        if "fab_chlg" in url or "fab_chlg" in combined:
            return True
        if "fab_" in url or "fab_" in combined:
            return True
        if "ограничил доступ" in combined or "доступ ограничен" in combined:
            return True
        if any(marker in combined for marker in BLOCK_MARKERS):
            return True
        if "выключите vpn" in text and "перезагрузите роутер" in text:
            return True
        if "инцидент:" in combined and "fab_" in combined:
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
    if not is_access_restricted(page):
        return True

    incident = extract_incident_id(page)
    if is_antibot_challenge_page(page):
        log("Ozon проверяет браузер; автоматические повторы остановлены")
    else:
        log("Ozon заблокировал текущую страницу; автоматические повторы остановлены")
    if incident:
        log(f"Инцидент: {incident}")

    if not on_manual_bypass or on_manual_bypass(incident) is False:
        log("Ручное подтверждение не получено; переход отменён")
        return False

    if is_access_restricted(page):
        log("Проверка или блокировка ещё активна. Дождитесь загрузки Ozon в Chrome")
        return False

    log(
        f"Доступ восстановлен. Защитная пауза {int(BLOCK_COOLDOWN_MIN)}–"
        f"{int(BLOCK_COOLDOWN_MAX)} сек..."
    )
    human_delay(BLOCK_COOLDOWN_MIN, BLOCK_COOLDOWN_MAX)
    wait_for_ozon_ready(page, log)
    return not is_access_restricted(page)


def _normalize_url(url: str) -> str:
    return url.split("#")[0].strip().rstrip("/")


def safe_goto(
    page: Page,
    url: str,
    progress=None,
    max_retries: int | None = None,
    on_manual_bypass: Callable[[str | None], bool | None] | None = None,
) -> bool:
    log = progress or (lambda _m: None)
    retries = SAFE_GOTO_MAX_RETRIES if max_retries is None else max(1, max_retries)
    target = _normalize_url(url)
    for attempt in range(1, retries + 1):
        try:
            if is_blocked_page(page):
                incident = extract_incident_id(page)
                log(
                    "Страница уже заблокирована Ozon"
                    + (f" ({incident})" if incident else "")
                    + "; переход отменён без повторных запросов"
                )
                return recover_access(
                    page,
                    log,
                    on_manual_bypass,
                    target_url=url,
                )

            current = _normalize_url(page.url)
            if current != target:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
            human_delay(2.0, 4.0)
            if not wait_for_ozon_ready(page, log):
                if is_blocked_page(page):
                    incident = extract_incident_id(page)
                    if incident:
                        log(f"Инцидент при загрузке: {incident}")
                    # Never auto-reload fab_/incident pages — that prolongs the block.
                    return recover_access(
                        page,
                        log,
                        on_manual_bypass,
                        target_url=url,
                    )
                if is_antibot_challenge_page(page):
                    log("Ozon проверяет браузер — подтвердите в Chrome и нажмите «Продолжить»")
                    if recover_access(
                        page,
                        log,
                        on_manual_bypass,
                        target_url=url,
                    ) and wait_for_ozon_ready(page, log):
                        if not is_access_restricted(page):
                            return True
                    return False
                if is_captcha_page(page):
                    return recover_access(
                        page,
                        log,
                        on_manual_bypass,
                        target_url=url,
                    )
                return False

            if is_access_restricted(page):
                return recover_access(
                    page,
                    log,
                    on_manual_bypass,
                    target_url=url,
                )
            return True
        except Exception as exc:
            log(f"Ошибка загрузки: {exc}")
            if attempt < retries and not is_access_restricted(page):
                human_delay(3.0, 5.0)
            else:
                return False
    return False
