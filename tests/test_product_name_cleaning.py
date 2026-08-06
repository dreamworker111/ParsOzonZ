"""Product title cleaning: drop listing badges like «1 шт» / «распродажа»."""

from __future__ import annotations

import unittest

from ozon_parser.parser import OzonParser, ParseSettings, _ParseState
from ozon_parser.utils import clean_product_name, is_product_name_noise, pick_product_name


class ProductNameCleaningTests(unittest.TestCase):
    def test_noise_badges_detected(self):
        self.assertTrue(is_product_name_noise("1 шт"))
        self.assertTrue(is_product_name_noise("распродажа"))
        self.assertTrue(is_product_name_noise("Хит"))
        self.assertTrue(is_product_name_noise("Осталось 3 шт"))
        self.assertTrue(is_product_name_noise("Бренд проверен"))
        self.assertTrue(is_product_name_noise("Проверенный бренд"))
        self.assertFalse(is_product_name_noise("Кабель USB Type-C 1м"))

    def test_clean_strips_trailing_badges(self):
        self.assertEqual(
            clean_product_name("Кабель USB 1 шт распродажа"),
            "Кабель USB",
        )
        self.assertEqual(clean_product_name("1 шт"), "")
        self.assertEqual(clean_product_name("распродажа"), "")
        self.assertEqual(clean_product_name("Бренд проверен"), "")

    def test_pick_prefers_real_title_over_badges(self):
        text = "\n".join(
            [
                "распродажа",
                "1 шт",
                "Наушники беспроводные TWS",
                "1 990 ₽",
                "100 баллов за отзыв",
            ]
        )
        self.assertEqual(pick_product_name(text), "Наушники беспроводные TWS")

    def test_pick_ignores_brand_verified_badge(self):
        text = "\n".join(
            [
                "Бренд проверен",
                "Шампунь питательный 400 мл",
                "890 ₽",
                "50 баллов за отзыв",
            ]
        )
        self.assertEqual(
            pick_product_name("Бренд проверен", text),
            "Шампунь питательный 400 мл",
        )

    def test_process_card_ignores_badge_as_name(self):
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
        card = {
            "href": "https://www.ozon.ru/product/headphones-42/",
            "name": "1 шт",
            "text": "распродажа\n1 шт\nНаушники беспроводные TWS\n1 990 ₽",
            "html": "<div>распродажа</div><div>1 шт</div><a title='Наушники беспроводные TWS'>…</a><div>1 990 ₽</div>",
        }
        product = parser._process_card(card, settings, _ParseState())
        self.assertIsNotNone(product)
        self.assertEqual(product.name, "Наушники беспроводные TWS")
        self.assertNotIn("шт", product.name.lower())
        self.assertNotIn("распродажа", product.name.lower())

    def test_process_card_rejects_brand_verified_as_title(self):
        parser = OzonParser()
        settings = ParseSettings(
            seller_url="https://www.ozon.ru/seller/shop-1/",
            categories=[],
            min_price=None,
            max_price=None,
            max_products=10,
            use_auth=False,
            import_browser_session=False,
            bonus_only=False,
        )
        card = {
            "href": "https://www.ozon.ru/product/shampun-pitanie-i-sila-1112973437/",
            "name": "Бренд проверен",
            "text": (
                "Бренд проверен\n"
                "SHAMTU Шампунь для волос женский Питание и сила, 500 мл\n"
                "208 ₽\n395 ₽"
            ),
            "html": (
                "<div>Бренд проверен</div>"
                "<a title='SHAMTU Шампунь для волос женский Питание и сила, 500 мл'>товар</a>"
                "<div>208 ₽</div>"
            ),
        }
        product = parser._process_card(card, settings, _ParseState())
        self.assertIsNotNone(product)
        self.assertEqual(
            product.name,
            "SHAMTU Шампунь для волос женский Питание и сила, 500 мл",
        )
        self.assertNotIn("бренд", product.name.lower())

    def test_process_card_cleans_glued_badges_in_name(self):
        parser = OzonParser()
        settings = ParseSettings(
            seller_url="",
            categories=[],
            min_price=None,
            max_price=None,
            max_products=10,
            use_auth=False,
            import_browser_session=False,
            bonus_only=True,
        )
        card = {
            "href": "https://www.ozon.ru/product/item-77/",
            "name": "Гель для стирки 1 шт распродажа",
            "text": "Гель для стирки 1 шт распродажа\n890 ₽\n50 баллов за отзыв",
            "html": "<div>50 баллов за отзыв</div>",
        }
        product = parser._process_card(card, settings, _ParseState())
        self.assertIsNotNone(product)
        self.assertEqual(product.name, "Гель для стирки")


if __name__ == "__main__":
    unittest.main()
