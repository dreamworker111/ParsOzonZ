"""Tests for safe start of global catalogue parsing with ~1000 categories."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ozon_parser.categories import CategoryTarget
from ozon_parser.config import (
    DESKTOP_MODE,
    GLOBAL_LARGE_SELECTION_THRESHOLD,
    GLOBAL_SESSION_MAX_CATEGORIES,
)
from ozon_parser.parser import OzonParser, ParseSettings


def _make_targets(count: int) -> list[CategoryTarget]:
    targets: list[CategoryTarget] = []
    for i in range(count):
        cid = str(10000 + i)
        targets.append(
            CategoryTarget(
                id=f"Категория|category:{cid}",
                name=f"Категория {cid}",
                url=f"https://www.ozon.ru/category/{cid}/",
                section="Категория",
                param_key="category",
                param_value=cid,
                category_id=cid,
            )
        )
    return targets


class GlobalBulkParseBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = OzonParser()
        self.parser._session_mode = "cdp"

    def test_large_global_selection_uses_single_category_session(self):
        settings = ParseSettings(
            seller_url="",
            categories=_make_targets(1000),
            min_price=None,
            max_price=None,
            max_products=10000,
            use_auth=False,
            import_browser_session=True,
            use_cdp=True,
            browser_mode=DESKTOP_MODE,
            specific_seller=False,
        )
        cats, products = self.parser._session_budgets(settings)
        self.assertEqual(cats, 1)
        self.assertLessEqual(products, 80)

    def test_small_global_selection_keeps_global_budget(self):
        settings = ParseSettings(
            seller_url="",
            categories=_make_targets(max(1, GLOBAL_LARGE_SELECTION_THRESHOLD - 1)),
            min_price=None,
            max_price=None,
            max_products=500,
            use_auth=False,
            import_browser_session=True,
            specific_seller=False,
        )
        cats, products = self.parser._session_budgets(settings)
        self.assertEqual(cats, GLOBAL_SESSION_MAX_CATEGORIES)
        self.assertLessEqual(products, 80)

    def test_all_thousand_urls_use_seller_filter(self):
        for target in _make_targets(1000):
            url = self.parser._build_global_catalog_url(target, DESKTOP_MODE)
            self.assertIn("/seller/", url)
            self.assertIn(f"category={target.category_id}", url)
            self.assertNotIn(f"/category/{target.category_id}", url)


class GlobalBulkParseStartTests(unittest.TestCase):
    def test_run_opens_only_session_budget_for_1000_categories(self):
        targets = _make_targets(1000)
        settings = ParseSettings(
            seller_url="",
            categories=targets,
            min_price=None,
            max_price=None,
            max_products=1000,
            use_auth=False,
            import_browser_session=True,
            use_cdp=True,
            browser_mode=DESKTOP_MODE,
            specific_seller=False,
        )

        page = Mock()
        page.url = "https://www.ozon.ru/seller/"
        page.title.return_value = "Ozon"
        page.evaluate.return_value = "товары категории"
        page.query_selector.return_value = None

        opened_urls: list[str] = []

        def fake_safe_goto(page_obj, url, progress=None, max_retries=None, on_manual_bypass=None):
            opened_urls.append(url)
            page_obj.url = url
            return True

        parser = OzonParser(on_progress=lambda _m: None)
        parser._session_mode = "cdp"

        with (
            patch("ozon_parser.parser.sync_playwright") as sync_pw,
            patch.object(parser, "_open_browser", return_value=(None, Mock(), page, "cdp")),
            patch("ozon_parser.parser.ensure_ozon_session_ready", return_value=True),
            patch("ozon_parser.parser.is_access_restricted", return_value=False),
            patch("ozon_parser.parser.is_seller_page", return_value=True),
            patch("ozon_parser.parser.safe_goto", side_effect=fake_safe_goto),
            patch("ozon_parser.parser.human_delay"),
            patch("ozon_parser.parser.human_category_delay"),
            patch.object(parser, "_protective_pause", return_value=True),
            patch.object(parser, "_extract_product_cards", return_value=[]),
            patch.object(parser, "_ensure_price_sort_asc"),
            patch("ozon_parser.parser.save_checkpoint"),
            patch("ozon_parser.parser.load_checkpoint", return_value=None),
            patch("ozon_parser.parser.clear_checkpoint"),
            patch("ozon_parser.parser.close_session_context"),
        ):
            sync_pw.return_value.__enter__.return_value = Mock()
            products, _stats = parser.run(settings)

        self.assertEqual(products, [])
        # Only one category navigation for large global selection.
        self.assertEqual(len(opened_urls), 1)
        self.assertIn("/seller/", opened_urls[0])
        self.assertIn("category=", opened_urls[0])
        self.assertNotIn("/category/", opened_urls[0].split("?")[0])

    def test_fab_block_stops_without_retry_navigation(self):
        targets = _make_targets(1000)
        settings = ParseSettings(
            seller_url="",
            categories=targets[:5],
            min_price=None,
            max_price=None,
            max_products=100,
            use_auth=False,
            import_browser_session=True,
            specific_seller=False,
        )

        page = Mock()
        page.url = "https://www.ozon.ru/seller/"
        page.title.return_value = "Похоже, нет соединения"
        page.evaluate.return_value = (
            "Похоже, нет соединения\nВыключите VPN\nИнцидент: fab_20260802064211_TEST"
        )

        goto_calls = {"count": 0}

        def fake_safe_goto(page_obj, url, progress=None, max_retries=None, on_manual_bypass=None):
            goto_calls["count"] += 1
            page_obj.url = url
            return False

        logs: list[str] = []
        parser = OzonParser(on_progress=logs.append)
        parser._session_mode = "cdp"

        with (
            patch("ozon_parser.parser.sync_playwright") as sync_pw,
            patch.object(parser, "_open_browser", return_value=(None, Mock(), page, "cdp")),
            patch("ozon_parser.parser.ensure_ozon_session_ready", return_value=True),
            patch(
                "ozon_parser.parser.is_access_restricted",
                side_effect=[False, False, True, True, True],
            ),
            patch("ozon_parser.parser.is_seller_page", return_value=True),
            patch("ozon_parser.parser.safe_goto", side_effect=fake_safe_goto),
            patch("ozon_parser.parser.human_delay"),
            patch.object(parser, "_protective_pause", return_value=True),
            patch("ozon_parser.parser.save_checkpoint"),
            patch("ozon_parser.parser.load_checkpoint", return_value=None),
            patch("ozon_parser.parser.clear_checkpoint"),
            patch("ozon_parser.parser.close_session_context"),
        ):
            sync_pw.return_value.__enter__.return_value = Mock()
            products, _stats = parser.run(settings)

        self.assertEqual(products, [])
        self.assertEqual(goto_calls["count"], 1)
        self.assertTrue(any("fab_" in msg or "блокиров" in msg.lower() for msg in logs))


if __name__ == "__main__":
    unittest.main()
