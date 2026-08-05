"""One physical Ozon product must appear once in results."""

from __future__ import annotations

import unittest

from ozon_parser.export import ProductRow
from ozon_parser.parser import OzonParser, ParseSettings, _ParseState
from ozon_parser.utils import extract_ozon_product_id


class ProductDedupeTests(unittest.TestCase):
    def test_extract_product_id_from_slug_urls(self):
        self.assertEqual(
            extract_ozon_product_id("https://www.ozon.ru/product/cable-usb-1678901234/"),
            "1678901234",
        )
        self.assertEqual(
            extract_ozon_product_id("https://www.ozon.ru/product/1678901234/?o=1"),
            "1678901234",
        )
        self.assertEqual(
            extract_ozon_product_id("/product/name-a-1678901234"),
            "1678901234",
        )

    def test_dedupe_products_collapses_same_id_different_slugs(self):
        parser = OzonParser()
        products = [
            ProductRow("A", 100, 120, "https://www.ozon.ru/product/foo-111222333/", 10),
            ProductRow("A copy", 100, 120, "https://www.ozon.ru/product/bar-111222333/", 10),
            ProductRow("B", 200, 200, "https://www.ozon.ru/product/other-999888777/", 0),
        ]
        unique = parser._dedupe_products(products)
        self.assertEqual(len(unique), 2)
        ids = {extract_ozon_product_id(p.url) for p in unique}
        self.assertEqual(ids, {"111222333", "999888777"})

    def test_process_card_skips_second_slug_of_same_product(self):
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
        )
        state = _ParseState()
        first = {
            "href": "https://www.ozon.ru/product/alpha-555666777/",
            "name": "Товар А",
            "text": "Товар А\n500 ₽",
            "html": "",
        }
        second = {
            "href": "https://www.ozon.ru/product/beta-555666777/",
            "name": "Товар А",
            "text": "Товар А\n500 ₽",
            "html": "",
        }
        self.assertIsNotNone(parser._process_card(first, settings, state))
        self.assertIsNone(parser._process_card(second, settings, state))
        self.assertEqual(state.seen_product_ids, {"555666777"})

    def test_merge_cards_by_product_id(self):
        merged = OzonParser._merge_product_cards(
            [
                {
                    "href": "https://www.ozon.ru/product/a-123456789/",
                    "name": "Кабель",
                    "text": "Кабель\n100 ₽\n200 ₽\n300 ₽\n400 ₽\n500 ₽\n600 ₽\n700 ₽",
                    "html": "",
                }
            ],
            [
                {
                    "href": "https://www.ozon.ru/product/b-123456789/",
                    "name": "Кабель USB",
                    "text": "Кабель USB\n100 ₽",
                    "html": "",
                }
            ],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["name"], "Кабель USB")
        self.assertLessEqual(merged[0]["text"].count("₽"), 2)


if __name__ == "__main__":
    unittest.main()
