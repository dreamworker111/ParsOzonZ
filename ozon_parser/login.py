import threading

from playwright.sync_api import BrowserContext, Error as PlaywrightError, sync_playwright

from .auth import mark_mobile_session_saved, mobile_browser_profile_dir, save_storage_state
from .browser import connect_via_cdp, create_persistent_context
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


class MobileBrowserLoginSession:
    """Interactive mobile login in an isolated persistent Chromium profile."""

    def run(self, on_progress=None) -> bool:
        progress = on_progress or (lambda _m: None)
        try:
            with sync_playwright() as playwright:
                context, page = create_persistent_context(
                    playwright,
                    headless=False,
                    user_data_dir=mobile_browser_profile_dir(),
                    browser_mode=MOBILE_MODE,
                )
                if "ozon.ru" not in page.url:
                    page.goto(MOBILE_BASE_URL, wait_until="domcontentloaded", timeout=60000)

                progress(
                    "Мобильный Ozon открыт в отдельном окне. "
                    "Войдите в аккаунт и закройте окно браузера."
                )
                while context.pages:
                    try:
                        page.wait_for_timeout(500)
                    except PlaywrightError:
                        break

                mark_mobile_session_saved()
                progress("Мобильный профиль сохранён")
                return True
        except Exception as exc:
            progress(f"Ошибка мобильного входа: {exc}")
            return False


def login_mobile_via_browser(on_progress=None) -> bool:
    return MobileBrowserLoginSession().run(on_progress=on_progress)
