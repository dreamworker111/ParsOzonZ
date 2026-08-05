"""Regression: catalog scroll must not stop early on virtualized grids."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ozon_parser.parser import OzonParser
from ozon_parser.export import ProductRow


class CatalogScrollCompletenessTests(unittest.TestCase):
    def test_scroll_continues_when_height_flat_but_position_moves(self):
        """Virtualized Ozon grids often keep scrollHeight constant."""
        page = Mock()
        page.query_selector.return_value = None
        metrics = [
            {"y": 0, "h": 3000, "ih": 900, "products": 12},
            {"y": 820, "h": 3000, "ih": 900, "products": 12},
        ]

        parser = OzonParser(on_progress=lambda _m: None)
        with (
            patch.object(parser, "_catalog_scroll_metrics", side_effect=metrics),
            patch.object(parser, "_wait_page_load_state"),
            patch.object(
                parser,
                "_wait_catalog_batch_settle",
                side_effect=lambda page, **kwargs: metrics[min(len(metrics) - 1, 1)],
            ),
            patch("ozon_parser.parser.is_blocked_page", return_value=False),
            patch("ozon_parser.parser.human_delay"),
        ):
            self.assertTrue(parser._scroll_for_more(page))
        page.evaluate.assert_called()

    def test_scroll_stops_only_at_true_bottom(self):
        page = Mock()
        page.query_selector.return_value = None
        # After step and bottom nudge: still at end, no growth.
        metrics = [
            {"y": 2100, "h": 3000, "ih": 900, "products": 36},
            {"y": 2100, "h": 3000, "ih": 900, "products": 36},
            {"y": 2100, "h": 3000, "ih": 900, "products": 36},
        ]
        parser = OzonParser(on_progress=lambda _m: None)
        with (
            patch.object(parser, "_catalog_scroll_metrics", side_effect=metrics),
            patch.object(parser, "_click_catalog_load_more", return_value=False),
            patch.object(parser, "_wait_page_load_state"),
            patch.object(
                parser,
                "_wait_catalog_batch_settle",
                side_effect=lambda page, **kwargs: {"y": 2100, "h": 3000, "ih": 900, "products": 36},
            ),
            patch("ozon_parser.parser.is_blocked_page", return_value=False),
            patch("ozon_parser.parser.human_delay"),
        ):
            self.assertFalse(parser._scroll_for_more(page))

    def test_batch_settle_waits_until_metrics_stable(self):
        page = Mock()
        sequence = [
            {"y": 0, "h": 2000, "ih": 900, "products": 10},
            {"y": 0, "h": 2600, "ih": 900, "products": 18},
            {"y": 0, "h": 2600, "ih": 900, "products": 18},
            {"y": 0, "h": 2600, "ih": 900, "products": 18},
        ]
        parser = OzonParser(on_progress=lambda _m: None)
        with (
            patch.object(parser, "_catalog_scroll_metrics", side_effect=sequence),
            patch("ozon_parser.parser.is_access_restricted", return_value=False),
            patch.object(parser._stop_event, "wait", return_value=False),
        ):
            settled = parser._wait_catalog_batch_settle(
                page, timeout_sec=5.0, stable_checks=2
            )
        self.assertEqual(settled["products"], 18)
        self.assertEqual(settled["h"], 2600)

    def test_ensure_catalog_page_fully_loaded_waits_for_tiles(self):
        page = Mock()
        logs: list[str] = []
        parser = OzonParser(on_progress=logs.append)
        with (
            patch.object(parser, "_wait_page_load_state") as wait_load,
            patch("ozon_parser.parser.wait_for_ozon_ready", return_value=True),
            patch.object(parser, "_wait_catalog_listing_ready", return_value=True) as wait_ready,
            patch.object(
                parser,
                "_wait_catalog_batch_settle",
                return_value={"y": 0, "h": 2000, "ih": 900, "products": 24},
            ),
            patch.object(
                parser,
                "_catalog_scroll_metrics",
                return_value={"y": 0, "h": 2000, "ih": 900, "products": 24},
            ),
            patch("ozon_parser.parser.is_access_restricted", return_value=False),
            patch("ozon_parser.parser.human_delay"),
        ):
            parser._ensure_catalog_page_fully_loaded(page)
        wait_load.assert_called()
        wait_ready.assert_called()
        self.assertTrue(any("полной загрузки" in msg for msg in logs))
        self.assertTrue(any("24 карточек" in msg for msg in logs))

    def test_merge_product_cards_keeps_richer_fields(self):
        dom = [
            {
                "href": "https://www.ozon.ru/product/a-1/",
                "name": "A",
                "text": "A\n100 ₽",
                "html": "",
            }
        ]
        composer = [
            {
                "href": "https://www.ozon.ru/product/a-1/?o=1",
                "name": "Товар A полный",
                "text": "Товар A полный\n100 ₽\n50 баллов за отзыв",
                "html": "<div>50 баллов за отзыв</div>",
            },
            {
                "href": "https://www.ozon.ru/product/b-2/",
                "name": "B",
                "text": "B\n200 ₽",
                "html": "",
            },
        ]
        merged = OzonParser._merge_product_cards(dom, composer)
        by_href = {c["href"]: c for c in merged}
        self.assertEqual(len(merged), 2)
        self.assertIn("https://www.ozon.ru/product/a-1/", by_href)
        self.assertIn("50 баллов", by_href["https://www.ozon.ru/product/a-1/"]["text"])
        self.assertEqual(by_href["https://www.ozon.ru/product/a-1/"]["name"], "Товар A полный")

    def test_process_card_reads_price_from_html_when_text_empty(self):
        from ozon_parser.parser import ParseSettings, _ParseState
        from ozon_parser.config import DESKTOP_MODE

        parser = OzonParser()
        settings = ParseSettings(
            seller_url="",
            categories=[],
            min_price=None,
            max_price=None,
            max_products=10,
            use_auth=False,
            import_browser_session=False,
            bonus_only=False,
            browser_mode=DESKTOP_MODE,
        )
        card = {
            "href": "https://www.ozon.ru/product/html-only-9/",
            "name": "",
            "text": "",
            "html": "<div><span>Кабель USB</span><span>1 490 ₽</span></div>",
        }
        product = parser._process_card(card, settings, _ParseState())
        self.assertIsInstance(product, ProductRow)
        self.assertEqual(product.name, "Кабель USB")
        self.assertEqual(product.price_discounted, 1490.0)


if __name__ == "__main__":
    unittest.main()
