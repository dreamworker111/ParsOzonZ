"""fab_ recovery: park tab, wait without reload, then probe."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ozon_parser.config import DESKTOP_MODE, PARSE_MODE_GLOBAL_CATEGORIES
from ozon_parser.parser import OzonParser, ParseSettings


class FabWaitParkTests(unittest.TestCase):
    def test_wait_out_parks_blank_and_never_reloads(self):
        settings = ParseSettings(
            seller_url="",
            categories=[],
            min_price=None,
            max_price=None,
            max_products=10,
            use_auth=False,
            import_browser_session=True,
            parse_mode=PARSE_MODE_GLOBAL_CATEGORIES,
        )
        page = Mock()
        page.url = "https://www.ozon.ru/seller/0/?category=15500"
        page.reload = Mock(side_effect=AssertionError("reload must not run on fab_"))

        goto_urls: list[str] = []

        def fake_goto(url, **kwargs):
            goto_urls.append(url)
            page.url = url

        page.goto = fake_goto

        logs: list[str] = []
        parser = OzonParser(on_progress=logs.append)
        parser._page = page
        parser._block_auto_waits_used = 0

        with (
            patch("ozon_parser.parser.BLOCK_AUTO_WAIT_SEC", 0.05),
            patch("ozon_parser.parser.BLOCK_AUTO_WAIT_POLL_SEC", 0.01),
            patch("ozon_parser.parser.BLOCK_POST_CLEAR_COOLDOWN_MIN", 0.0),
            patch("ozon_parser.parser.BLOCK_POST_CLEAR_COOLDOWN_MAX", 0.0),
            patch("ozon_parser.parser.extract_incident_id", return_value="fab_TEST"),
            patch("ozon_parser.parser.human_delay"),
            patch("ozon_parser.parser.safe_goto", return_value=True) as safe_goto,
            patch("ozon_parser.parser.is_access_restricted", return_value=False),
        ):
            ok = parser._wait_out_access_block(
                settings,
                reopen_url="https://www.ozon.ru/seller/0/?category=15500",
            )

        self.assertTrue(ok)
        self.assertIn("about:blank", goto_urls)
        page.reload.assert_not_called()
        safe_goto.assert_called_once()
        self.assertTrue(
            any("about:blank" in msg or "без F5" in msg for msg in logs)
        )

    def test_wait_out_returns_false_when_probe_still_blocked(self):
        settings = ParseSettings(
            seller_url="",
            categories=[],
            min_price=None,
            max_price=None,
            max_products=10,
            use_auth=False,
            import_browser_session=True,
            parse_mode=PARSE_MODE_GLOBAL_CATEGORIES,
        )
        page = Mock()
        page.url = "https://www.ozon.ru/"
        page.goto = Mock()

        parser = OzonParser(on_progress=lambda _m: None)
        parser._page = page
        parser._block_auto_waits_used = 0

        with (
            patch("ozon_parser.parser.BLOCK_AUTO_WAIT_SEC", 0.05),
            patch("ozon_parser.parser.BLOCK_AUTO_WAIT_POLL_SEC", 0.01),
            patch("ozon_parser.parser.extract_incident_id", return_value="fab_TEST"),
            patch("ozon_parser.parser.human_delay"),
            patch("ozon_parser.parser.safe_goto", return_value=False),
        ):
            ok = parser._wait_out_access_block(settings)

        self.assertFalse(ok)

    def test_seed_shop_roots_skips_view_all_when_enough_known(self):
        from ozon_parser.categories import CategoryLoader

        loader = CategoryLoader(page=Mock(), browser_mode=DESKTOP_MODE)
        loader._seller_root_ids = {str(i) for i in range(10)}
        logs: list[str] = []

        with (
            patch.object(loader, "_extract_seller_filter_children") as filter_mock,
            patch.object(loader, "_open_category_filter") as open_mock,
            patch.object(loader, "_ensure_shop_all_categories_opened") as all_mock,
            patch.object(loader, "_ensure_category_view_all_opened") as view_mock,
        ):
            loader._seed_shop_root_ids("https://www.ozon.ru/seller/shop-1/", logs.append)

        filter_mock.assert_not_called()
        open_mock.assert_not_called()
        all_mock.assert_not_called()
        view_mock.assert_not_called()
        self.assertTrue(any("уже известны" in msg for msg in logs))


if __name__ == "__main__":
    unittest.main()
