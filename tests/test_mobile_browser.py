import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import browser_cookie3  # noqa: F401
except ModuleNotFoundError:
    cookie_module = types.ModuleType("browser_cookie3")
    cookie_module.chrome = lambda **_kwargs: []
    cookie_module.edge = lambda **_kwargs: []
    sys.modules["browser_cookie3"] = cookie_module

try:
    import playwright.sync_api  # noqa: F401
except ModuleNotFoundError:
    playwright_module = types.ModuleType("playwright")
    sync_api_module = types.ModuleType("playwright.sync_api")
    for name in ("Browser", "BrowserContext", "Page", "Playwright"):
        setattr(sync_api_module, name, type(name, (), {}))
    sys.modules["playwright"] = playwright_module
    sys.modules["playwright.sync_api"] = sync_api_module

from ozon_parser import auth, browser
from ozon_parser.config import (
    DESKTOP_USER_AGENT,
    DESKTOP_VIEWPORT,
    MOBILE_MODE,
    MOBILE_USER_AGENT,
    MOBILE_VIEWPORT,
)


class BrowserModeTests(unittest.TestCase):
    def test_desktop_options_remain_unchanged(self):
        options = browser.browser_context_options()

        self.assertEqual(options["user_agent"], DESKTOP_USER_AGENT)
        self.assertEqual(options["viewport"], DESKTOP_VIEWPORT)
        self.assertNotIn("is_mobile", options)
        self.assertNotIn("has_touch", options)

    def test_mobile_options_emulate_android_touch_device(self):
        options = browser.browser_context_options(MOBILE_MODE)

        self.assertIn("Android", MOBILE_USER_AGENT)
        self.assertIn("Chrome/", MOBILE_USER_AGENT)
        self.assertEqual(options["viewport"], MOBILE_VIEWPORT)
        self.assertTrue(options["is_mobile"])
        self.assertTrue(options["has_touch"])
        self.assertGreater(options["device_scale_factor"], 1)

    @patch("ozon_parser.browser.create_browser_context")
    def test_mobile_guest_ignores_cookies_and_cdp(self, create_context):
        create_context.return_value = ("browser", "context", "page")
        playwright = object()

        result = browser.open_session_context(
            playwright,
            headless=True,
            storage_state={"cookies": [{"name": "desktop"}]},
            use_cdp=True,
            browser_mode=MOBILE_MODE,
            use_auth=False,
        )

        self.assertEqual(result, ("browser", "context", "page", "mobile_guest"))
        create_context.assert_called_once_with(
            playwright,
            True,
            storage_state=None,
            browser_mode=MOBILE_MODE,
        )

    @patch("ozon_parser.browser.create_persistent_context")
    @patch("ozon_parser.browser.has_mobile_saved_session", return_value=False)
    def test_mobile_auth_requires_saved_profile(self, _has_session, create_context):
        with self.assertRaisesRegex(RuntimeError, "Мобильная сессия не найдена"):
            browser.open_session_context(
                object(),
                headless=False,
                browser_mode=MOBILE_MODE,
                use_auth=True,
            )
        create_context.assert_not_called()

    def test_unknown_browser_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            browser.browser_context_options("tablet")  # type: ignore[arg-type]

    def test_mobile_auth_requires_live_ozon_token(self):
        context = type(
            "Context",
            (),
            {
                "cookies": lambda _self: [
                    {
                        "name": "__Secure-access-token",
                        "value": "token",
                        "domain": ".ozon.ru",
                        "expires": time.time() + 60,
                    },
                    {
                        "name": "__Secure-user-id",
                        "value": "42",
                        "domain": ".ozon.ru",
                        "expires": time.time() + 60,
                    },
                ]
            },
        )()
        self.assertTrue(browser.mobile_context_is_authenticated(context))

        context.cookies = lambda: [
            {
                "name": "__Secure-access-token",
                "value": "token",
                "domain": ".ozon.ru",
                "expires": time.time() + 60,
            },
            {
                "name": "__Secure-user-id",
                "value": "0",
                "domain": ".ozon.ru",
                "expires": time.time() + 60,
            },
        ]
        self.assertFalse(browser.mobile_context_is_authenticated(context))

        context.cookies = lambda: [
            {
                "name": "__Secure-access-token",
                "value": "token",
                "domain": ".ozon.ru",
                "expires": time.time() - 60,
            },
            {
                "name": "__Secure-user-id",
                "value": "42",
                "domain": ".ozon.ru",
                "expires": time.time() + 60,
            },
        ]
        self.assertFalse(browser.mobile_context_is_authenticated(context))

    def test_blocked_navigation_waits_for_manual_confirmation(self):
        page = Mock()
        page.url = "https://m.ozon.ru/"
        page.evaluate.return_value = "Подтвердите, что вы не робот"
        confirmation = Mock(return_value=False)

        with patch("ozon_parser.browser.human_delay"):
            result = browser.safe_goto(
                page,
                page.url,
                max_retries=1,
                on_manual_bypass=confirmation,
            )

        self.assertFalse(result)
        confirmation.assert_called_once_with(None)
        page.reload.assert_not_called()


class MobileProfileStateTests(unittest.TestCase):
    def test_mobile_session_marker_requires_initialized_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "mobile_profile"
            marker = root / "mobile_session.json"
            session_dir = root / "session"

            with (
                patch.object(auth, "MOBILE_CHROME_PROFILE", profile),
                patch.object(auth, "MOBILE_SESSION_MARKER", marker),
                patch.object(auth, "SESSION_DIR", session_dir),
            ):
                self.assertFalse(auth.has_mobile_saved_session())
                (profile / "Default").mkdir(parents=True)
                auth.mark_mobile_session_saved()
                self.assertTrue(auth.has_mobile_saved_session())


if __name__ == "__main__":
    unittest.main()
