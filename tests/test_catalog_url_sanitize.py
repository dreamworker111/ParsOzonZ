"""Tests for clean catalog URLs and empty-filter recovery."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from ozon_parser.browser import is_empty_catalog_filter_page, try_reset_catalog_filters
from ozon_parser.config import DESKTOP_MODE, PARSE_MODE_SELLER_CATEGORIES
from ozon_parser.parser import OzonParser
from ozon_parser.categories import CategoryTarget
from ozon_parser.utils import sanitize_catalog_url


class SanitizeCatalogUrlTests(unittest.TestCase):
    def test_strips_stale_query_flags_from_seller_category_url(self):
        dirty = (
            "https://www.ozon.ru/seller/shop-123/?category=15500"
            "&opened=category-15500&layout_container=default&page=2&brand=999"
            "&sorting=price"
        )
        clean = sanitize_catalog_url(
            dirty,
            param_key="category",
            param_value="15500",
            browser_mode=DESKTOP_MODE,
        )
        self.assertIn("category=15500", clean)
        self.assertIn("sorting=price", clean)
        self.assertNotIn("opened=", clean)
        self.assertNotIn("layout_container=", clean)
        self.assertNotIn("page=", clean)
        self.assertNotIn("brand=", clean)

    def test_keeps_brand_filter_with_parent_category(self):
        url = "https://www.ozon.ru/seller/shop-123/"
        clean = sanitize_catalog_url(
            url,
            param_key="brand",
            param_value="26303000",
            category_id="15500",
            browser_mode=DESKTOP_MODE,
        )
        self.assertIn("category=15500", clean)
        self.assertIn("brand=26303000", clean)
        self.assertIn("sorting=price", clean)

    def test_catalog_url_for_target_ignores_stale_target_url(self):
        parser = OzonParser()
        target = CategoryTarget(
            id="Категория|category:15500",
            name="Электроника",
            url=(
                "https://www.ozon.ru/seller/shop/?category=15500"
                "&opened=category-15500&page=3"
            ),
            section="Категория",
            param_key="category",
            param_value="15500",
            category_id="15500",
        )
        from ozon_parser.parser import ParseSettings

        settings = ParseSettings(
            seller_url="https://www.ozon.ru/seller/shop-123/",
            categories=[target],
            min_price=None,
            max_price=None,
            max_products=10,
            use_auth=False,
            import_browser_session=False,
            parse_mode=PARSE_MODE_SELLER_CATEGORIES,
        )
        built = parser._catalog_url_for_target(settings.seller_url, target, settings)
        self.assertIn("category=15500", built)
        self.assertNotIn("opened=", built)
        self.assertNotIn("page=", built)


class EmptyCatalogPageTests(unittest.TestCase):
    def test_detects_reset_filters_empty_state(self):
        page = Mock()
        page.evaluate.return_value = {
            "text": (
                "не нашли товары в магазине по вашим параметрам ничего не нашлось "
                "попробуйте сбросить фильтры"
            ),
            "productLinks": 0,
        }
        self.assertTrue(is_empty_catalog_filter_page(page))

    def test_normal_catalog_is_not_empty_state(self):
        page = Mock()
        page.evaluate.return_value = {
            "text": "ozon каталог товары электроника смартфоны цена",
            "productLinks": 24,
        }
        self.assertFalse(is_empty_catalog_filter_page(page))

    def test_reset_chip_alone_is_not_empty_when_products_exist(self):
        page = Mock()
        page.evaluate.return_value = {
            "text": "электроника смартфоны сбросить фильтры 1 200 ₽",
            "productLinks": 18,
        }
        self.assertFalse(is_empty_catalog_filter_page(page))

    def test_try_reset_clicks_visible_button(self):
        page = Mock()
        button = Mock()
        button.is_visible.return_value = True
        page.query_selector.return_value = button
        self.assertTrue(try_reset_catalog_filters(page))
        button.click.assert_called_once()


if __name__ == "__main__":
    unittest.main()
