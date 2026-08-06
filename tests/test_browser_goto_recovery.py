"""Navigation recovery for CDP Chrome (net::ERR_FAILED)."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ozon_parser import browser


class BrowserGotoRecoveryTests(unittest.TestCase):
    def test_is_transient_goto_error(self) -> None:
        self.assertTrue(
            browser._is_transient_goto_error(
                RuntimeError("Page.goto: net::ERR_FAILED at https://www.ozon.ru/")
            )
        )
        self.assertFalse(
            browser._is_transient_goto_error(ValueError("invalid url"))
        )

    def test_goto_page_retries_after_err_failed(self) -> None:
        page = Mock()
        page.is_closed.return_value = False
        page.url = "about:blank"
        page.goto.side_effect = [
            RuntimeError("Page.goto: net::ERR_FAILED at https://www.ozon.ru/"),
            None,
        ]
        logs: list[str] = []

        with patch("ozon_parser.browser.human_delay"):
            ok = browser._goto_page(page, "https://www.ozon.ru/", logs.append)

        self.assertTrue(ok)
        self.assertGreaterEqual(page.goto.call_count, 2)

    def test_acquire_cdp_page_resets_error_tab(self) -> None:
        bad = Mock()
        bad.is_closed.return_value = False
        bad.url = "chrome-error://chromewebdata/"

        def _reset_blank(*_args, **_kwargs):
            bad.url = "about:blank"

        bad.goto.side_effect = _reset_blank
        context = Mock()
        context.pages = [bad]
        logs: list[str] = []

        page = browser._acquire_cdp_page(context, logs.append)

        self.assertIs(page, bad)
        bad.goto.assert_called_with("about:blank", wait_until="commit", timeout=20000)

    def test_log_ozon_network_failure_detects_block(self) -> None:
        page = Mock()
        logs: list[str] = []
        with patch("ozon_parser.browser.probe_chrome_network", return_value=(True, False)):
            browser.log_ozon_network_failure(page, logs.append, prefix="Не удалось открыть Ozon.")
        self.assertTrue(any("ozon.ru недоступен" in line for line in logs))

    def test_safe_goto_uses_recovery_helper(self) -> None:
        page = Mock()
        page.url = "about:blank"
        page.evaluate.return_value = "ozon каталог категории товары"

        with (
            patch("ozon_parser.browser._goto_page", return_value=True) as goto,
            patch("ozon_parser.browser.human_delay"),
            patch("ozon_parser.browser.wait_for_ozon_ready", return_value=True),
        ):
            ok = browser.safe_goto(page, "https://www.ozon.ru/seller/")

        self.assertTrue(ok)
        goto.assert_called_once()


if __name__ == "__main__":
    unittest.main()
