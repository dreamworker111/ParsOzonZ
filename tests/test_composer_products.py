"""Unit tests for Composer listing card extraction."""

from __future__ import annotations

import json
import unittest

from ozon_parser.composer_products import extract_product_cards_from_composer


class ComposerProductExtractTests(unittest.TestCase):
    def test_extracts_tile_grid_product(self):
        item = {
            "action": {"link": "/product/smartfon-test-123456789/"},
            "sku": 123456789,
            "mainState": [
                {
                    "type": "priceV2",
                    "priceV2": {"price": [{"text": "12 990 ₽", "textStyle": "PRICE"}]},
                },
                {
                    "type": "textDS",
                    "textDS": {"text": "Смартфон Test Phone"},
                    "id": "name",
                },
                {
                    "type": "labelListV2",
                    "labelListV2": {
                        "items": [{"type": "text", "text": {"text": "150 баллов за отзыв"}}]
                    },
                },
            ],
        }
        data = {
            "widgetStates": {
                "tileGridDesktop-1": json.dumps({"items": [item]}, ensure_ascii=False)
            }
        }
        cards = extract_product_cards_from_composer(data)
        self.assertEqual(len(cards), 1)
        self.assertIn("/product/", cards[0]["href"])
        self.assertIn("Смартфон", cards[0]["name"])
        self.assertIn("балл", cards[0]["text"].lower())

    def test_skips_non_product_nodes(self):
        data = {
            "widgetStates": {
                "filters-1": json.dumps({"title": "Категория", "values": ["A", "B"]})
            }
        }
        self.assertEqual(extract_product_cards_from_composer(data), [])


if __name__ == "__main__":
    unittest.main()
