import unittest

from ozon_parser.categories import CategoryTarget
from ozon_parser.config import DESKTOP_MODE, MOBILE_MODE
from ozon_parser.parser import OzonParser


class GlobalCatalogParseUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = OzonParser()
        self.parser._session_mode = "cdp"

    def test_global_url_uses_seller_filter_not_category_path(self):
        target = CategoryTarget(
            id="Категория|category:15500",
            name="Электроника",
            url="https://www.ozon.ru/category/15500/",
            category_id="15500",
            param_key="category",
            param_value="15500",
        )
        url = self.parser._build_global_catalog_url(target, DESKTOP_MODE)

        self.assertIn("/seller/", url)
        self.assertIn("category=15500", url)
        self.assertNotIn("/category/15500", url)
        self.assertIn("sorting=price", url)

    def test_global_url_resolves_id_from_tree_key(self):
        target = CategoryTarget(
            id="Категория|category:25000",
            name="Дом и сад",
            url="https://www.ozon.ru/category/25000/",
        )
        category_id = self.parser._resolve_global_category_id(target)

        self.assertEqual(category_id, "25000")

    def test_mobile_guest_cdp_keeps_www_host_for_global_parse(self):
        self.parser._session_mode = "mobile_guest_cdp"
        target = CategoryTarget(
            id="Категория|category:15500",
            category_id="15500",
            param_value="15500",
        )
        url = self.parser._build_global_catalog_url(target, MOBILE_MODE)

        self.assertIn("www.ozon.ru", url)
        self.assertIn("category=15500", url)
        self.assertNotIn("m.ozon.ru", url)


if __name__ == "__main__":
    unittest.main()
