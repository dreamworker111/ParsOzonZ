"""Regression: sibling shop roots must not nest under Электроника."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ozon_parser.categories import CategoryLoader, CategoryTarget
from ozon_parser.config import DESKTOP_MODE


class SellerSubcategorySiblingRootTests(unittest.TestCase):
    def test_shop_subtree_continues_to_api_when_dom_is_only_sibling_roots(self) -> None:
        page = Mock()
        page.url = "https://www.ozon.ru/seller/puladuo-4720087/?category=15500"
        loader = CategoryLoader(page, DESKTOP_MODE)
        loader._seller_root_ids = {"15500", "6500", "7500", "17777"}
        sibling_dom = [
            {"id": "6500", "name": "Красота и здоровье", "children": []},
            {"id": "7500", "name": "Одежда", "children": []},
        ]
        real_api = [{"id": "15543", "name": "Наушники и аудиотехника", "children": []}]

        with (
            patch("ozon_parser.categories.safe_goto", return_value=True),
            patch("ozon_parser.categories.fast_delay"),
            patch.object(loader, "_pause_after_page_open"),
            patch.object(loader, "_prepare_category_filter_panel"),
            patch.object(
                loader,
                "_ensure_category_view_all_opened",
                return_value={"present": True, "opened": True},
            ),
            patch.object(loader, "_extract_full_subtree_dom", return_value=sibling_dom),
            patch.object(loader, "_extract_seller_filter_children", return_value=[]),
            patch.object(loader, "_expand_category_subtree_in_filter"),
            patch.object(loader, "_extract_categories_from_wb6_block", return_value=sibling_dom),
            patch.object(loader, "_fetch_subcategories_via_api", return_value=real_api),
            patch.object(loader, "_build_category_url", return_value=page.url),
            patch.object(loader, "_cleanup_categories", side_effect=lambda x: x),
        ):
            result = loader._fetch_category_subtree(
                "https://www.ozon.ru/seller/puladuo-4720087/",
                {"id": "15500", "name": "Электроника"},
                lambda _m: None,
                None,
            )

        self.assertEqual([n["id"] for n in result], ["15543"])

    def test_fresh_subcat_load_excludes_sibling_roots(self) -> None:
        page = Mock()
        page.url = "https://www.ozon.ru/seller/puladuo-4720087/"
        loader = CategoryLoader(page, DESKTOP_MODE)

        dom_roots = [
            {"id": "15500", "name": "Электроника"},
            {"id": "7500", "name": "Одежда"},
            {"id": "6500", "name": "Красота и здоровье"},
            {"id": "17777", "name": "Обувь"},
            {"id": "14500", "name": "Дом и сад"},
            {"id": "7000", "name": "Детские товары"},
            {"id": "10500", "name": "Бытовая техника"},
            {"id": "11000", "name": "Спорт и отдых"},
        ]
        truncated_composer = dom_roots[:5]

        def fake_collect(seller_base, parent, **kwargs):
            dump = [
                {"id": "7500", "name": "Одежда", "children": []},
                {"id": "6500", "name": "Красота и здоровье", "children": []},
                {"id": "15543", "name": "Наушники и аудиотехника", "children": []},
            ]
            parent["children"] = loader._resolve_direct_child_seeds(
                dump,
                parent_id=str(parent["id"]),
                api_children=dump,
            )

        with (
            patch.object(loader, "_ensure_seller_shop_open"),
            patch.object(loader, "_open_category_filter"),
            patch.object(loader, "_ensure_shop_all_categories_opened", return_value=False),
            patch.object(
                loader,
                "_ensure_category_view_all_opened",
                return_value={"present": False},
            ),
            patch.object(loader, "_extract_categories_from_wb6_block", return_value=dom_roots),
            patch.object(
                loader,
                "_extract_seller_filter_children",
                return_value=truncated_composer,
            ),
            patch.object(loader, "_collect_category_branch_whole", side_effect=fake_collect),
            patch(
                "ozon_parser.categories.normalize_seller_url",
                return_value="https://www.ozon.ru/seller/puladuo-4720087/",
            ),
        ):
            mapping = loader.load_subcategories_for_categories(
                "https://www.ozon.ru/seller/puladuo-4720087/",
                [
                    CategoryTarget(
                        id="x",
                        name="Электроника",
                        param_value="15500",
                        category_id="15500",
                    )
                ],
            )

        self.assertGreaterEqual(len(loader._seller_root_ids), 8)
        self.assertIn("6500", loader._seller_root_ids)
        kids = mapping["15500"]
        self.assertEqual([c.param_value for c in kids], ["15543"])
        self.assertTrue(all("красота" not in (c.name or "").lower() for c in kids))
        self.assertTrue(all("одежда" not in (c.name or "").lower() for c in kids))


if __name__ == "__main__":
    unittest.main()
