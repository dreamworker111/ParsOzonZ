import json
import unittest
from unittest.mock import Mock, patch

from ozon_parser.categories import CategoryLoader, CategoryTarget
from ozon_parser.category_extract import extract_direct_children as extract_app_children
from ozon_parser.config import DESKTOP_MODE, MOBILE_MODE
from ozon_parser.parser import OzonParser, ParseSettings
from ozon_parser.seller_category_collector import SellerCategoryCollector
from ozon_categories.parser import extract_direct_children as extract_fallback_children
from ozon_parser.utils import (
    normalize_seller_url,
    to_browser_url,
    with_price_sort_asc,
)


class MobileRoutingTests(unittest.TestCase):
    def test_mode_routing_preserves_path_query_and_fragment(self):
        source = "https://www.ozon.ru/seller/shop-1/?category=123#items"

        mobile = to_browser_url(source, MOBILE_MODE)
        desktop = to_browser_url(mobile, DESKTOP_MODE)

        self.assertEqual(
            mobile,
            "https://m.ozon.ru/seller/shop-1/?category=123#items",
        )
        self.assertEqual(desktop, source)

    def test_mobile_seller_normalization_and_sorting_stay_mobile(self):
        seller = normalize_seller_url(
            "https://www.ozon.ru/seller/shop-1",
            MOBILE_MODE,
        )
        catalog = with_price_sort_asc(seller, MOBILE_MODE)

        self.assertEqual(seller, "https://m.ozon.ru/seller/shop-1/")
        self.assertIn("m.ozon.ru", catalog)
        self.assertIn("sorting=price", catalog)

    def test_parsers_never_infer_children_when_parent_is_missing(self):
        tree = [
            {"id": "100", "name": "Электроника", "children": []},
            {
                "id": "200",
                "name": "Одежда",
                "children": [
                    {"id": "201", "name": "Мужская одежда", "children": []}
                ],
            },
        ]

        self.assertEqual(
            extract_app_children(
                tree,
                "999",
                root_ids={"100", "200"},
                page_category_id="999",
            ),
            [],
        )
        self.assertEqual(
            extract_fallback_children(
                tree,
                "999",
                root_ids={"100", "200"},
                page_category_id="999",
            ),
            [],
        )


class MobileComposerTests(unittest.TestCase):
    def test_mobile_collector_reads_mobile_filter_widget_and_levels(self):
        collector = SellerCategoryCollector(
            Mock(),
            "https://www.ozon.ru/seller/shop-1/",
            browser_mode=MOBILE_MODE,
        )
        state = {
            "sections": [
                {
                    "filters": [
                        {
                            "type": "categoryFilter",
                            "key": "category",
                            "categoryFilter": {
                                "categories": [
                                    {
                                        "title": "Root",
                                        "urlValue": "?category=100",
                                        "level": 0,
                                    },
                                    {
                                        "title": "Child",
                                        "urlValue": "?category=101",
                                        "level": 1,
                                    },
                                ]
                            },
                        }
                    ]
                }
            ]
        }
        composer = {
            "widgetStates": {
                "filtersMobile-1": json.dumps(state),
            }
        }

        roots = collector._extract_category_filter_children(composer, None)
        collector._root_ids = {"100"}
        collector._known_ids = {"100"}
        children = collector._extract_category_filter_children(composer, "100")

        self.assertEqual([item["id"] for item in roots], ["100"])
        self.assertEqual([item["id"] for item in children], ["101"])

    def test_missing_parent_does_not_turn_categories_into_children(self):
        collector = SellerCategoryCollector(
            Mock(),
            "https://www.ozon.ru/seller/",
            browser_mode=DESKTOP_MODE,
        )
        collector._root_ids = {"100", "200"}
        collector._known_ids = {"100", "200"}
        state = {
            "sections": [
                {
                    "filters": [
                        {
                            "type": "categoryFilter",
                            "key": "category",
                            "categoryFilter": {
                                "categories": [
                                    {
                                        "title": "Электроника",
                                        "urlValue": "?category=100",
                                        "level": 0,
                                    },
                                    {
                                        "title": "Одежда",
                                        "urlValue": "?category=200",
                                        "level": 0,
                                    },
                                    {
                                        "title": "Мужская одежда",
                                        "urlValue": "?category=201",
                                        "level": 1,
                                    },
                                ]
                            },
                        }
                    ]
                }
            ]
        }
        composer = {"widgetStates": {"filtersDesktop-1": json.dumps(state)}}

        children = collector._extract_category_filter_children(composer, "999")

        self.assertEqual(children, [])

    def test_collector_rejects_root_alias_as_subcategory(self):
        collector = SellerCategoryCollector(
            Mock(),
            "https://www.ozon.ru/seller/",
            browser_mode=DESKTOP_MODE,
        )
        collector._open_page = Mock(return_value=True)
        root_one = {"id": "100", "name": "Электроника", "url": "?category=100"}
        root_two = {"id": "200", "name": "Одежда", "url": "?category=200"}
        responses = {
            None: [root_one, root_two],
            "100": [
                {"id": "999", "name": "Электроника", "url": "?category=999"},
                {"id": "101", "name": "Смартфоны", "url": "?category=101"},
            ],
            "101": [],
            "200": [],
        }
        collector._collect_direct_children = Mock(
            side_effect=lambda parent_id, page_url: responses.get(parent_id, [])
        )

        roots = collector.collect()

        self.assertEqual([root.name for root in roots], ["Электроника", "Одежда"])
        self.assertEqual(
            [child.name for child in roots[0].children],
            ["Смартфоны"],
        )
        self.assertEqual(roots[1].children, [])

    def test_mobile_composer_uses_mobile_host_and_client(self):
        page = Mock()
        page.evaluate.return_value = {}
        collector = SellerCategoryCollector(
            page,
            "https://www.ozon.ru/seller/shop-1/",
            browser_mode=MOBILE_MODE,
        )

        collector._fetch_composer("https://www.ozon.ru/seller/shop-1/?category=1")

        payload = page.evaluate.call_args.args[1]
        self.assertIn("m.ozon.ru", payload["fullUrl"])
        self.assertEqual(payload["clientName"], "mweb_client")


class ParserModeTests(unittest.TestCase):
    def test_parse_settings_default_keeps_desktop_behavior(self):
        settings = ParseSettings(
            seller_url="https://www.ozon.ru/seller/shop-1/",
            categories=[],
            min_price=None,
            max_price=None,
            max_products=1,
            use_auth=False,
            import_browser_session=True,
        )

        self.assertEqual(settings.browser_mode, DESKTOP_MODE)

    @patch("ozon_parser.parser.resolve_storage_state")
    @patch("ozon_parser.parser.open_session_context")
    def test_open_browser_propagates_mobile_auth(
        self,
        open_context,
        resolve_state,
    ):
        open_context.return_value = (None, "context", "page", "mobile_persistent")
        parser = OzonParser()

        parser._open_browser(
            object(),
            use_auth=True,
            use_cdp=True,
            import_browser=True,
            browser_mode=MOBILE_MODE,
        )

        resolve_state.assert_not_called()
        self.assertEqual(open_context.call_args.kwargs["browser_mode"], MOBILE_MODE)
        self.assertTrue(open_context.call_args.kwargs["use_auth"])

    def test_mobile_card_fallback_uses_mobile_widget_containers(self):
        page = Mock()
        page.evaluate.return_value = []

        OzonParser()._extract_product_cards(page, MOBILE_MODE)

        payload = page.evaluate.call_args.args[1]
        self.assertIn(
            '[data-widget*="product"]',
            payload["containerSelectors"],
        )

    def test_category_loader_routes_mobile_urls(self):
        loader = CategoryLoader(Mock(), MOBILE_MODE)

        self.assertEqual(
            loader._route_url("https://www.ozon.ru/product/item-1/"),
            "https://m.ozon.ru/product/item-1/",
        )

    @patch("ozon_parser.categories.safe_goto", return_value=True)
    def test_global_catalog_loader_collects_full_tree(self, _safe_goto):
        loader = CategoryLoader(Mock(), DESKTOP_MODE)
        loader._load_global_root_categories = Mock(
            return_value=[
                {
                    "id": "100",
                    "name": "Электроника",
                    "url": "/category/elektronika-100/",
                    "children": [],
                }
            ]
        )
        loader._fetch_categories_batch = Mock(
            side_effect=[
                {
                    "100": [
                        {
                            "id": "101",
                            "name": "Смартфоны",
                            "url": "/category/smartfony-101/",
                            "children": [],
                        }
                    ]
                },
                {"101": []},
            ]
        )
        roots = loader.load_global_category_tree()

        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].category_id, "100")
        self.assertEqual(len(roots[0].children), 1)
        self.assertEqual(roots[0].children[0].category_id, "101")
        self.assertEqual(loader._fetch_categories_batch.call_count, 2)
        # Subtree Composer API must use /category/ base to return children.
        self.assertIn(
            "/category/",
            loader._fetch_categories_batch.call_args_list[0].args[0],
        )

    def test_global_catalog_error_explains_ozon_block(self):
        page = Mock()
        page.url = "https://www.ozon.ru/seller/"
        page.title.return_value = "Похоже, нет соединения"
        page.evaluate.return_value = (
            "Похоже, нет соединения\nИнцидент: fab_20260801204409_TEST"
        )
        loader = CategoryLoader(page, DESKTOP_MODE)
        error = loader._global_catalog_access_error("fallback")
        message = str(error).lower()
        self.assertTrue(
            "заблокировал" in message or "ограничил" in message,
            message,
        )
        self.assertIn("инцидент", message)
        self.assertIn("15–30", message)

    def test_build_category_url_uses_category_path_for_global_tree(self):
        loader = CategoryLoader(Mock(), DESKTOP_MODE)
        url = loader._build_category_url("https://www.ozon.ru/category/", "15500")
        self.assertEqual(url, "https://www.ozon.ru/category/15500/")
        path, full = loader._composer_urls_for_category(
            "https://www.ozon.ru/category/",
            "15500",
        )
        self.assertEqual(path, "/category/15500/")
        self.assertEqual(full, "https://www.ozon.ru/category/15500/")

    def test_build_category_url_uses_seller_filter_for_shop(self):
        loader = CategoryLoader(Mock(), DESKTOP_MODE)
        url = loader._build_category_url(
            "https://www.ozon.ru/seller/shop-1/",
            "15500",
        )
        self.assertEqual(
            url,
            "https://www.ozon.ru/seller/shop-1/?category=15500",
        )

    def test_all_stores_uses_seller_filter_url(self):
        target = CategoryTarget(
            id="123",
            category_id="123",
            url="https://www.ozon.ru/seller/shop-1/?category=123",
        )

        url = OzonParser()._build_global_catalog_url(target, DESKTOP_MODE)

        self.assertEqual(
            url,
            "https://www.ozon.ru/seller/?category=123&sorting=price",
        )

        with_category_path = CategoryTarget(
            id="15500",
            category_id="15500",
            url="https://www.ozon.ru/category/elektronika-15500/",
        )
        self.assertEqual(
            OzonParser()._build_global_catalog_url(with_category_path, DESKTOP_MODE),
            "https://www.ozon.ru/seller/?category=15500&sorting=price",
        )


if __name__ == "__main__":
    unittest.main()
