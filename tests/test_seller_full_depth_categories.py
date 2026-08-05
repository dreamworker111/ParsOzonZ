"""Seller category deep walk uses page + «Посмотреть все» at full depth."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ozon_parser.categories import CategoryLoader, CategoryTarget
from ozon_parser.config import DESKTOP_MODE, SELLER_CATEGORY_PAGE_MAX_DEPTH


class SellerFullDepthCategoryTests(unittest.TestCase):
    def test_seller_subcategories_use_full_page_depth(self) -> None:
        page = Mock()
        loader = CategoryLoader(page, DESKTOP_MODE, session_mode="cdp")
        targets = [
            CategoryTarget(
                id="Категория|category:15500",
                name="Электроника",
                param_key="category",
                param_value="15500",
                category_id="15500",
                url="https://www.ozon.ru/seller/shop-1/?category=15500",
            )
        ]
        captured: dict = {}

        def fake_collect(seller_base, parent, **kwargs):
            captured.update(kwargs)
            parent["children"] = [
                {
                    "id": "15501",
                    "name": "Телефоны",
                    "url": None,
                    "children": [
                        {"id": "15502", "name": "Смартфоны", "url": None, "children": []}
                    ],
                }
            ]

        with (
            patch.object(loader, "_collect_category_branch_whole", side_effect=fake_collect),
            patch.object(loader, "_ensure_seller_shop_open"),
            patch.object(loader, "_seed_shop_root_ids"),
            patch(
                "ozon_parser.categories.normalize_seller_url",
                return_value="https://www.ozon.ru/seller/shop-1/",
            ),
        ):
            mapping = loader.load_subcategories_for_categories(
                "https://www.ozon.ru/seller/shop-1/",
                targets,
            )

        self.assertEqual(captured.get("page_max_depth"), SELLER_CATEGORY_PAGE_MAX_DEPTH)
        self.assertGreaterEqual(SELLER_CATEGORY_PAGE_MAX_DEPTH, 10)
        self.assertIn("15500", mapping)
        self.assertEqual(mapping["15500"][0].param_value, "15501")
        self.assertEqual(mapping["15500"][0].children[0].param_value, "15502")

    def test_seller_roots_open_view_all(self) -> None:
        page = Mock()
        page.url = "https://www.ozon.ru/seller/shop-1/"
        loader = CategoryLoader(page, DESKTOP_MODE, session_mode="cdp")
        view_all = Mock(return_value={"present": True, "opened": True})

        with (
            patch.object(loader, "_ensure_seller_shop_open"),
            patch.object(loader, "_wait_for_filters"),
            patch.object(loader, "_open_category_filter") as open_filter,
            patch.object(loader, "_ensure_shop_all_categories_opened", return_value=True),
            patch.object(loader, "_ensure_category_view_all_opened", view_all),
            patch.object(loader, "_expand_filter_sections") as expand,
            patch.object(
                loader,
                "_collect_root_categories",
                return_value=[{"id": "1", "name": "Электроника", "children": []}],
            ),
            patch.object(
                loader,
                "_category_dict_to_option",
                side_effect=lambda data, parent_name="": Mock(
                    param_value=data["id"], category_id=data["id"], name=data["name"]
                ),
            ),
        ):
            roots = loader.load_root_categories("https://www.ozon.ru/seller/shop-1/")

        open_filter.assert_called_once()
        view_all.assert_called_once()
        expand.assert_called_once()
        self.assertEqual(len(roots), 1)


if __name__ == "__main__":
    unittest.main()
