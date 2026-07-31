import json
import unittest
from unittest.mock import Mock, patch

from ozon_parser.categories import CategoryLoader
from ozon_parser.config import DESKTOP_MODE, MOBILE_MODE
from ozon_parser.parser import OzonParser, ParseSettings
from ozon_parser.seller_category_collector import SellerCategoryCollector
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


if __name__ == "__main__":
    unittest.main()
