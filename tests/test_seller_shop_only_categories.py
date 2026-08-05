"""Seller category load must use shop filter only — not the global Ozon tree."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from ozon_parser.categories import CategoryLoader
from ozon_parser.config import DESKTOP_MODE
from ozon_parser.seller_category_collector import SellerCategoryCollector


def _filter_state(categories: list[dict], widget_key: str = "filtersDesktop-1") -> dict:
    state = {
        "sections": [
            {
                "filters": [
                    {
                        "type": "categoryFilter",
                        "key": "category",
                        "categoryFilter": {"categories": categories},
                    }
                ]
            }
        ]
    }
    return {"widgetStates": {widget_key: json.dumps(state)}}


class SellerShopOnlyCategoryTests(unittest.TestCase):
    def test_is_seller_shop_base(self) -> None:
        loader = CategoryLoader(Mock(), DESKTOP_MODE)
        self.assertTrue(loader._is_seller_shop_base("https://www.ozon.ru/seller/shop-123/"))
        self.assertFalse(loader._is_seller_shop_base("https://www.ozon.ru/seller/"))
        self.assertFalse(loader._is_seller_shop_base("https://www.ozon.ru/seller/0/"))
        self.assertFalse(loader._is_seller_shop_base("https://www.ozon.ru/category/15500/"))

    def test_collect_roots_skips_global_composer_tree(self) -> None:
        page = Mock()
        page.url = "https://www.ozon.ru/seller/my-shop-999/"
        loader = CategoryLoader(page, DESKTOP_MODE)
        shop_roots = [{"id": "10", "name": "Только в магазине", "children": []}]

        with (
            patch.object(loader, "_extract_seller_filter_children", return_value=shop_roots) as filt,
            patch.object(loader, "_extract_categories_from_dom", return_value=[]),
            patch.object(loader, "_extract_categories_from_wb6_block", return_value=[]),
            patch.object(
                loader,
                "_extract_categories_from_composer_api",
                return_value=[{"id": "1", "name": "Глобальная", "children": []}],
            ) as global_api,
            patch.object(loader, "_extract_categories_from_page_json", return_value=[]),
        ):
            roots = loader._collect_root_categories(lambda _m: None)

        filt.assert_called_once()
        global_api.assert_not_called()
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["id"], "10")

    def test_collect_roots_prefers_full_modal_over_truncated_filter(self) -> None:
        page = Mock()
        page.url = "https://www.ozon.ru/seller/puladuo-4720087/"
        loader = CategoryLoader(page, DESKTOP_MODE)
        truncated = [
            {"id": "200", "name": "Одежда", "children": []},
            {"id": "201", "name": "Женская одежда", "children": []},
        ]
        full_roots = [
            {"id": "100", "name": "Электроника", "children": []},
            {"id": "200", "name": "Одежда", "children": []},
            {"id": "300", "name": "Обувь", "children": []},
            {"id": "400", "name": "Дом и сад", "children": []},
            {"id": "500", "name": "Красота", "children": []},
        ]

        with (
            patch.object(loader, "_extract_seller_filter_children", return_value=truncated),
            patch.object(loader, "_extract_categories_from_dom", return_value=truncated),
            patch.object(loader, "_extract_categories_from_wb6_block", return_value=full_roots),
        ):
            roots = loader._collect_root_categories(lambda _m: None)

        self.assertEqual(len(roots), 5)
        self.assertEqual([r["id"] for r in roots], ["100", "200", "300", "400", "500"])
        self.assertTrue(all(r.get("children") == [] for r in roots))

    def test_category_link_utils_parse_seller_path_urls(self) -> None:
        """Shop filter links look like /seller/shop/slug-15500/ without 'category'."""
        from ozon_parser.categories import _CATEGORY_LINK_UTILS_JS

        self.assertIn(r"\/seller\/", _CATEGORY_LINK_UTILS_JS)
        self.assertIn("countCategoryAnchors", _CATEGORY_LINK_UTILS_JS)

        # Mirror JS extractCategoryId in Python for regression guard.
        import re

        def extract(href: str) -> str | None:
            for pattern in (
                r"[?&]category=(\d+)",
                r"/category/[^/?#]*-(\d+)/?(?:[?#]|$)",
                r"/category/(\d+)/?(?:[?#]|$)",
                r"/seller/[^/?#]+/[^/?#]*-(\d+)/?(?:[?#]|$)",
            ):
                m = re.search(pattern, href, re.I)
                if m:
                    return m.group(1)
            return None

        self.assertEqual(
            extract("/seller/puladuo-4720087/elektronika-15500/"),
            "15500",
        )
        self.assertEqual(
            extract("/seller/puladuo-4720087/krasota-i-zdorove-6500/"),
            "6500",
        )
        self.assertEqual(extract("?category=7500"), "7500")
        self.assertIsNone(extract("/seller/puladuo-4720087/"))

    def test_sibling_shop_roots_never_become_electronics_children(self) -> None:
        """Regression: «Красота и здоровье» must not appear under «Электроника»."""
        page = Mock()
        page.url = "https://www.ozon.ru/seller/puladuo-4720087/elektronika-15500/"
        loader = CategoryLoader(page, DESKTOP_MODE)
        loader._seller_root_ids = {
            "15500", "7500", "6500", "17777", "14500", "7000",
        }
        # DOM after «Посмотреть все» dumps sibling roots + one real child.
        dom_dump = [
            {"id": "7500", "name": "Одежда", "children": []},
            {"id": "6500", "name": "Красота и здоровье", "children": []},
            {"id": "17777", "name": "Обувь", "children": []},
            {"id": "15543", "name": "Наушники и аудиотехника", "children": []},
        ]
        seeds = loader._resolve_direct_child_seeds(
            dom_dump,
            parent_id="15500",
            api_children=[
                {"id": "6500", "name": "Красота и здоровье", "children": []},
                {"id": "15543", "name": "Наушники и аудиотехника", "children": []},
            ],
        )
        self.assertEqual([s["id"] for s in seeds], ["15543"])
        self.assertEqual(seeds[0]["name"], "Наушники и аудиотехника")

    def test_drop_sibling_shop_roots_filters_nested(self) -> None:
        loader = CategoryLoader(Mock(), DESKTOP_MODE)
        loader._seller_root_ids = {"15500", "6500", "7500"}
        tree = [
            {
                "id": "15543",
                "name": "Наушники",
                "children": [
                    {"id": "6500", "name": "Красота и здоровье", "children": []},
                    {"id": "15547", "name": "Наушники вкладыши", "children": []},
                ],
            },
            {"id": "7500", "name": "Одежда", "children": []},
        ]
        cleaned = loader._drop_sibling_shop_roots(tree, "15500")
        self.assertEqual([n["id"] for n in cleaned], ["15543"])
        self.assertEqual([n["id"] for n in cleaned[0]["children"]], ["15547"])

    def test_remember_shop_root_ids_never_shrinks(self) -> None:
        loader = CategoryLoader(Mock(), DESKTOP_MODE)
        loader._seller_root_ids = {"15500", "6500", "7500"}
        loader._remember_shop_root_ids([{"id": "15500"}, {"id": "7500"}])
        self.assertEqual(loader._seller_root_ids, {"15500", "6500", "7500"})
        loader._remember_shop_root_ids([{"id": "10500"}])
        self.assertIn("10500", loader._seller_root_ids)
        self.assertIn("6500", loader._seller_root_ids)

    def test_fetch_batch_skips_global_tree_fallback_for_shop(self) -> None:
        page = Mock()
        loader = CategoryLoader(page, DESKTOP_MODE)
        seller = "https://www.ozon.ru/seller/shop-1/"
        fake_payload = {
            "/seller/shop-1/?category=15500": {"widgetStates": {}},
        }

        with (
            patch("ozon_parser.categories.is_access_restricted", return_value=False),
            patch("ozon_parser.categories.is_blocked_page", return_value=False),
            patch("ozon_parser.categories.is_antibot_challenge_page", return_value=False),
            patch.object(
                loader,
                "_composer_urls_for_category",
                return_value=("/seller/shop-1/?category=15500", seller + "?category=15500"),
            ),
            patch.object(page, "evaluate", return_value=json.dumps(fake_payload)),
            patch.object(
                loader,
                "_seller_filter_collector",
                return_value=Mock(
                    _extract_category_filter_deeper_ids=Mock(return_value=set()),
                    _extract_category_filter_children=Mock(return_value=[]),
                ),
            ),
            patch("ozon_parser.categories.parse_composer_response") as parse_global,
        ):
            result = loader._fetch_categories_batch(seller, ["15500"], lambda _m: None)

        parse_global.assert_not_called()
        self.assertEqual(result.get("15500"), [])

    def test_fetch_composer_json_keeps_category_query(self) -> None:
        page = Mock()
        page.evaluate.return_value = None
        loader = CategoryLoader(page, DESKTOP_MODE)
        loader._fetch_composer_json(
            "https://www.ozon.ru/seller/shop-1/?category=15500"
        )
        payload = page.evaluate.call_args.args[1]
        self.assertIn("category=15500", payload["path"])
        self.assertIn("category=15500", payload["fullUrl"])

    def test_parent_absent_on_category_page_returns_children(self) -> None:
        collector = SellerCategoryCollector(
            Mock(),
            "https://www.ozon.ru/seller/shop-1/",
            browser_mode=DESKTOP_MODE,
        )
        collector._root_ids = {"100", "200"}
        composer = _filter_state(
            [
                {"title": "Смартфоны", "urlValue": "?category=101", "level": 0},
                {"title": "Планшеты", "urlValue": "?category=102", "level": 0},
            ],
            widget_key="searchFilters-321",
        )
        children = collector._extract_category_filter_children(
            composer,
            "100",
            page_category_id="100",
        )
        self.assertEqual([c["id"] for c in children], ["101", "102"])

    def test_missing_parent_without_page_id_stays_empty(self) -> None:
        collector = SellerCategoryCollector(
            Mock(),
            "https://www.ozon.ru/seller/shop-1/",
            browser_mode=DESKTOP_MODE,
        )
        collector._root_ids = {"100", "200"}
        composer = _filter_state(
            [
                {"title": "Электроника", "urlValue": "?category=100", "level": 0},
                {"title": "Одежда", "urlValue": "?category=200", "level": 0},
            ]
        )
        children = collector._extract_category_filter_children(composer, "999")
        self.assertEqual(children, [])

    def test_ensure_opens_target_shop_when_another_seller_is_open(self) -> None:
        page = Mock()
        page.url = "https://www.ozon.ru/seller/other-shop/"
        loader = CategoryLoader(page, DESKTOP_MODE)
        with (
            patch("ozon_parser.categories.is_access_restricted", return_value=False),
            patch("ozon_parser.categories.is_blocked_page", return_value=False),
            patch("ozon_parser.categories.safe_goto", return_value=True) as goto,
            patch.object(loader, "_pause_after_page_open"),
        ):
            loader._ensure_seller_shop_open(
                "https://www.ozon.ru/seller/my-shop/",
                lambda _m: None,
                None,
            )
        goto.assert_called_once()
        self.assertIn("/seller/my-shop/", goto.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
