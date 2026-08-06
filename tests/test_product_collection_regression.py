"""Regression: product collection must work for global category mode."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from ozon_parser.categories import CategoryTarget
from ozon_parser.config import DESKTOP_MODE, PARSE_MODE_GLOBAL_CATEGORIES
from ozon_parser.parser import OzonParser, ParseSettings


class GlobalProductCollectionRegressionTests(unittest.TestCase):
    def test_collects_bonus_products_in_global_category_mode(self) -> None:
        target = CategoryTarget(
            id="Категория|category:15500",
            name="Электроника",
            url="https://www.ozon.ru/category/15500/",
            section="Категория",
            param_key="category",
            param_value="15500",
            category_id="15500",
        )
        settings = ParseSettings(
            seller_url="",
            categories=[target],
            min_price=None,
            max_price=None,
            max_products=5,
            use_auth=False,
            import_browser_session=True,
            use_cdp=True,
            browser_mode=DESKTOP_MODE,
            parse_mode=PARSE_MODE_GLOBAL_CATEGORIES,
        )
        page = Mock()
        page.url = "https://www.ozon.ru/seller/"
        page.title.return_value = "Ozon"
        page.evaluate.return_value = {"text": "товары", "productLinks": 12}
        page.query_selector.return_value = None

        opened: list[str] = []

        def fake_goto(page_obj, url, progress=None, max_retries=None, on_manual_bypass=None):
            opened.append(url)
            page_obj.url = url
            return True

        cards = [
            {
                "href": "https://www.ozon.ru/product/item-1/",
                "name": "Товар с баллами",
                "text": "Товар с баллами\n1 200 ₽\n150 баллов за отзыв",
                "html": "<div>150 баллов за отзыв</div>",
            }
        ]
        logs: list[str] = []
        parser = OzonParser(on_progress=logs.append)
        parser._session_mode = "cdp"

        with ExitStack() as stack:
            sync_pw = stack.enter_context(patch("ozon_parser.parser.sync_playwright"))
            stack.enter_context(
                patch.object(parser, "_open_browser", return_value=(None, Mock(), page, "cdp"))
            )
            stack.enter_context(patch("ozon_parser.parser.ensure_ozon_session_ready", return_value=True))
            stack.enter_context(patch("ozon_parser.parser.is_access_restricted", return_value=False))
            stack.enter_context(patch("ozon_parser.parser.is_seller_page", return_value=True))
            stack.enter_context(
                patch("ozon_parser.parser.is_empty_catalog_filter_page", return_value=False)
            )
            stack.enter_context(patch("ozon_parser.parser.is_blocked_page", return_value=False))
            stack.enter_context(patch("ozon_parser.parser.is_captcha_page", return_value=False))
            stack.enter_context(patch("ozon_parser.parser.safe_goto", side_effect=fake_goto))
            stack.enter_context(patch("ozon_parser.parser.human_delay"))
            stack.enter_context(patch("ozon_parser.parser.human_category_delay"))
            stack.enter_context(patch("ozon_parser.parser.human_click_delay"))
            stack.enter_context(patch("ozon_parser.parser.human_scroll_delay"))
            stack.enter_context(patch.object(parser, "_protective_pause", return_value=True))
            stack.enter_context(patch.object(parser, "_wait_out_access_block", return_value=True))
            stack.enter_context(patch.object(parser, "_ensure_catalog_page_fully_loaded"))
            stack.enter_context(patch.object(parser, "_finalize_catalog_page_load", return_value=False))
            stack.enter_context(patch.object(parser, "_wait_catalog_batch_settle", return_value={}))
            stack.enter_context(patch.object(parser, "_extract_product_cards", return_value=cards))
            stack.enter_context(patch.object(parser, "_scroll_for_more", return_value=False))
            stack.enter_context(patch.object(parser, "_ensure_price_sort_asc"))
            stack.enter_context(patch("ozon_parser.parser.save_checkpoint"))
            stack.enter_context(patch("ozon_parser.parser.load_checkpoint", return_value=None))
            stack.enter_context(patch("ozon_parser.parser.clear_checkpoint"))
            stack.enter_context(patch("ozon_parser.parser.close_session_context"))
            sync_pw.return_value.__enter__.return_value = Mock()
            products, _stats = parser.run(settings)

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].bonus_points, 150)
        self.assertTrue(any("/category/15500" in url for url in opened))
        self.assertFalse(any("Пустая выдача" in msg for msg in logs))


if __name__ == "__main__":
    unittest.main()
