import threading
from typing import Callable

from playwright.sync_api import BrowserContext, Error as PlaywrightError, sync_playwright

from .auth import (
    mark_mobile_session_saved,
    mobile_browser_profile_dir,
    save_storage_state,
)
from .browser import (
    connect_via_cdp,
    create_persistent_context,
    is_blocked_page,
    is_captcha_page,
    settle_mobile_login,
    verify_mobile_authentication,
)
from .chrome_launcher import ensure_chrome_for_ozon
from .config import DESKTOP_BASE_URL, MOBILE_BASE_URL, MOBILE_MODE


class BrowserLoginSession:
    """Interactive login via real Chrome (CDP)."""

    def __init__(self):
        self._save_event = threading.Event()
        self._context: BrowserContext | None = None

    def request_save(self) -> None:
        self._save_event.set()

    def run(self, on_progress=None) -> bool:
        progress = on_progress or (lambda _m: None)
        self._save_event.clear()

        if not ensure_chrome_for_ozon(progress):
            progress("Chrome не найден. Установите Google Chrome.")
            return False

        progress("Подключение к Chrome...")

        try:
            with sync_playwright() as playwright:
                browser, context, page = connect_via_cdp(playwright, progress)
                self._context = context

                if "ozon.ru" not in page.url:
                    page.goto(DESKTOP_BASE_URL, wait_until="domcontentloaded", timeout=60000)

                progress(
                    "Chrome открыт. Дождитесь загрузки Ozon, "
                    "при необходимости пройдите проверку или войдите, "
                    "затем нажмите «Сохранить сессию»."
                )

                saved = self._save_event.wait(timeout=600)
                if not saved:
                    progress("Время ожидания истекло")
                    return False

                save_storage_state(context.storage_state())
                progress("Сессия сохранена")
                return True
        except Exception as exc:
            progress(f"Ошибка: {exc}")
            return False
        finally:
            self._context = None


def login_via_browser(on_progress=None) -> bool:
    session = BrowserLoginSession()
    return session.run(on_progress=on_progress)


def _wait_until_authenticated(context: BrowserContext, page, progress, seconds: float = 20.0) -> bool:
    """Poll cookies/UI briefly after the user confirms login."""
    steps = max(1, int(seconds / 0.5))
    for step in range(steps):
        if not context.pages:
            return False
        current = context.pages[-1]
        if verify_mobile_authentication(context, current):
            return True
        if step == 2:
            settle_mobile_login(current, progress)
        if is_captcha_page(current) or is_blocked_page(current):
            # Keep waiting: the user may still be finishing the challenge.
            pass
        try:
            current.wait_for_timeout(500)
        except PlaywrightError:
            return False
    return verify_mobile_authentication(context, context.pages[-1] if context.pages else page)


class MobileBrowserLoginSession:
    """Interactive mobile login in an isolated persistent Chromium profile."""

    def run(
        self,
        on_progress=None,
        wait_for_confirmation: Callable[[], bool] | None = None,
    ) -> bool:
        progress = on_progress or (lambda _m: None)
        context = None
        try:
            with sync_playwright() as playwright:
                context, page = create_persistent_context(
                    playwright,
                    headless=False,
                    user_data_dir=mobile_browser_profile_dir(),
                    browser_mode=MOBILE_MODE,
                )
                try:
                    if "ozon.ru" not in (page.url or ""):
                        page.goto(
                            MOBILE_BASE_URL,
                            wait_until="domcontentloaded",
                            timeout=60000,
                        )
                except PlaywrightError as exc:
                    progress(
                        "Ozon открыт, но страница ещё загружается. "
                        f"Пройдите проверку вручную, если она есть ({exc})."
                    )

                # Already logged in from a previous profile — save and finish.
                if verify_mobile_authentication(context, page):
                    mark_mobile_session_saved()
                    progress("Найдена действующая мобильная сессия")
                    return True

                progress(
                    "Мобильный Ozon открыт в отдельном окне. "
                    "Войдите в аккаунт, пройдите проверку Ozon, если она появилась, "
                    "и оставьте окно браузера открытым."
                )

                if wait_for_confirmation is not None:
                    while True:
                        if not wait_for_confirmation():
                            progress("Вход отменён")
                            return False
                        pages = context.pages
                        if not pages:
                            progress("Окно Ozon закрыто до проверки входа. Повторите вход.")
                            return False
                        page = pages[-1]

                        if is_captcha_page(page) or is_blocked_page(page):
                            progress(
                                "Проверка Ozon ещё не завершена. Пройдите её в окне браузера "
                                "и нажмите OK снова."
                            )
                            continue

                        if _wait_until_authenticated(context, page, progress):
                            mark_mobile_session_saved()
                            progress("Вход подтверждён, мобильная сессия сохранена")
                            return True

                        progress(
                            "Вход пока не обнаружен. Убедитесь, что открыт личный кабинет, "
                            "затем нажмите OK ещё раз."
                        )
                else:
                    # Compatibility path for callers without a confirmation UI:
                    # observe the state until the user closes the login window.
                    authenticated = False
                    while context.pages:
                        page = context.pages[-1]
                        authenticated = authenticated or verify_mobile_authentication(
                            context, page
                        )
                        try:
                            page.wait_for_timeout(500)
                        except PlaywrightError:
                            break
                    if not authenticated:
                        progress("Вход не подтверждён. Повторите попытку и завершите вход.")
                        return False

                mark_mobile_session_saved()
                progress("Вход подтверждён, мобильная сессия сохранена")
                return True
        except Exception as exc:
            progress(f"Не удалось открыть окно входа: {exc}")
            return False
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass


def login_mobile_via_browser(
    on_progress=None,
    wait_for_confirmation: Callable[[], bool] | None = None,
) -> bool:
    return MobileBrowserLoginSession().run(
        on_progress=on_progress,
        wait_for_confirmation=wait_for_confirmation,
    )
