import tempfile
import unittest
from pathlib import Path

from ozon_parser.categories import CategoryTarget
from ozon_parser.export import ProductRow
from ozon_parser.parse_checkpoint import (
    clear_checkpoint,
    load_checkpoint,
    save_checkpoint,
    target_key,
)
from ozon_parser.parser import ParseSettings


class ParseCheckpointTests(unittest.TestCase):
    def _settings(self, max_products: int = 10_000) -> ParseSettings:
        return ParseSettings(
            seller_url="",
            categories=[
                CategoryTarget(
                    id="15500",
                    category_id="15500",
                    name="Электроника",
                )
            ],
            min_price=None,
            max_price=None,
            max_products=max_products,
            use_auth=False,
            import_browser_session=False,
            specific_seller=False,
        )

    def test_checkpoint_round_trip_and_clear(self):
        settings = self._settings()
        target = settings.categories[0]
        product = ProductRow(
            name="Товар",
            price_discounted=100.0,
            price_original=120.0,
            url="https://www.ozon.ru/product/1/",
            bonus_points=50,
        )

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            save_checkpoint(settings, {target_key(target)}, [product], path)
            restored = load_checkpoint(settings, path)

            self.assertIsNotNone(restored)
            self.assertEqual(restored.completed_targets, {target_key(target)})
            self.assertEqual(restored.products, [product])

            clear_checkpoint(path)
            self.assertFalse(path.exists())

    def test_checkpoint_is_ignored_when_settings_change(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            save_checkpoint(self._settings(), set(), [], path)

            self.assertIsNone(load_checkpoint(self._settings(max_products=5000), path))

    def test_checkpoint_survives_reordered_same_targets(self):
        from ozon_parser.parse_checkpoint import settings_signature

        a = CategoryTarget(id="1", category_id="1", name="A")
        b = CategoryTarget(id="2", category_id="2", name="B")
        first = self._settings()
        first.categories = [a, b]
        second = self._settings()
        second.categories = [b, a]
        self.assertEqual(settings_signature(first), settings_signature(second))

    def test_session_budgets_shrink_for_large_goals(self):
        from ozon_parser.parser import OzonParser

        parser = OzonParser()
        cats, products = parser._session_budgets(self._settings(10_000))
        self.assertLessEqual(cats, 3)
        self.assertLessEqual(products, 250)

    def test_process_card_skips_detail_pages_in_bulk_mode(self):
        from unittest.mock import Mock
        from ozon_parser.parser import OzonParser, _ParseState

        parser = OzonParser()
        parser._fetch_product_detail = Mock(return_value=None)
        state = _ParseState()
        card = {
            "href": "https://www.ozon.ru/product/1/",
            "name": "Товар",
            "text": "Товар\n100 ₽\n50 баллов за отзыв",
            "html": "баллы",
        }
        product = parser._process_card(card, self._settings(), state)
        self.assertIsNotNone(product)
        parser._fetch_product_detail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
