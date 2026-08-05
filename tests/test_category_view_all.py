"""Tests for category filter «Посмотреть все» expansion."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ozon_parser.categories import (
    CATEGORY_SHOW_MORE_TEXTS,
    CATEGORY_VIEW_ALL_TEXTS,
    CategoryLoader,
)
from ozon_parser.category_extract import is_valid_category_name


class ViewAllCategoriesTests(unittest.TestCase):
    def test_tree_pace_heats_up_after_pressure(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        delays: list[tuple[float, float]] = []

        def capture(lo, hi):
            delays.append((lo, hi))

        with patch("ozon_parser.categories.human_delay", side_effect=capture):
            loader._batch_tree_pause()
            loader._note_tree_pressure(2)
            loader._batch_tree_pause()

        self.assertEqual(len(delays), 2)
        self.assertLess(delays[0][0], delays[1][0])
        self.assertLess(delays[0][1], delays[1][1])

    def test_show_more_texts_include_view_all(self) -> None:
        lowered = {t.lower() for t in CATEGORY_SHOW_MORE_TEXTS}
        self.assertIn("посмотреть все", lowered)
        self.assertIn("показать все", lowered)
        self.assertIn("посмотреть все", {t.lower() for t in CATEGORY_VIEW_ALL_TEXTS})

    def test_view_all_is_not_treated_as_category_name(self) -> None:
        self.assertFalse(is_valid_category_name("Посмотреть все"))
        self.assertFalse(is_valid_category_name("Показать все"))

    def test_click_category_show_more_scopes_to_category_section(self) -> None:
        page = Mock()
        page.evaluate.return_value = {
            "clicked": 1,
            "before": 5,
            "after": 16,
            "href": "",
            "modalOpen": False,
            "section": "категория",
        }
        page.wait_for_timeout = Mock()
        page.wait_for_selector = Mock()
        loader = CategoryLoader(page)
        with patch.object(
            loader,
            "_discover_category_show_more",
            return_value={"present": False, "href": "", "text": ""},
        ):
            clicks = loader._click_category_show_more(rounds=2)
        self.assertGreaterEqual(clicks, 1)
        self.assertTrue(page.evaluate.called)
        args = page.evaluate.call_args.args[1]
        self.assertIn("посмотреть все", args["viewAll"])
        self.assertIn("бренд", args["excludeSections"])
        # Script must use category-section finder, not whole filters panel.
        script = page.evaluate.call_args.args[0]
        self.assertIn("findViewAllInCategorySections", script)
        self.assertIn("findCategorySections", script)

    def test_merge_prefers_larger_tree(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        small = [{"id": "1", "name": "A", "children": []}]
        large = [
            {
                "id": "1",
                "name": "A",
                "children": [
                    {"id": "2", "name": "B", "children": []},
                    {"id": "3", "name": "C", "children": []},
                ],
            }
        ]
        merged = loader._merge_category_dict_lists(small, large)
        self.assertEqual(len(merged[0].get("children") or []), 2)

    def test_depth_seed_strips_shallow_dom_nesting(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        shallow = [
            {
                "id": "10",
                "name": "Phones",
                "url": "https://www.ozon.ru/category/10/",
                "children": [
                    {
                        "id": "11",
                        "name": "Smartphones",
                        "children": [{"id": "12", "name": "Android", "children": []}],
                    }
                ],
            },
            {"id": "20", "name": "Laptops", "children": []},
        ]
        seeds = loader._as_depth_seed_nodes(shallow)
        self.assertEqual([n["id"] for n in seeds], ["10", "20"])
        self.assertEqual(seeds[0]["children"], [])
        self.assertEqual(seeds[1]["children"], [])

    def test_resolve_seeds_prefers_composer_direct_children(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        # Flat modal dump: grandchildren mixed as siblings of real L1 children.
        flat_dom = [
            {"id": "10", "name": "Phones", "children": []},
            {"id": "11", "name": "Smartphones", "children": []},
            {"id": "12", "name": "Android", "children": []},
            {"id": "20", "name": "Laptops", "children": []},
        ]
        api_direct = [
            {"id": "10", "name": "Phones", "children": []},
            {"id": "20", "name": "Laptops", "children": []},
        ]
        seeds = loader._resolve_direct_child_seeds(
            flat_dom,
            parent_id="1",
            api_children=api_direct,
            exclude_ids={"11", "12"},
        )
        self.assertEqual([n["id"] for n in seeds], ["10", "20"])
        self.assertTrue(all(n["children"] == [] for n in seeds))

    def test_resolve_seeds_keeps_view_all_extras_over_truncated_composer(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        # Composer still truncated; DOM after «Посмотреть все» has the rest.
        dom_after_view_all = [
            {"id": "10", "name": "Phones", "children": []},
            {"id": "20", "name": "Laptops", "children": []},
            {"id": "30", "name": "Tablets", "children": []},
            {"id": "40", "name": "Audio", "children": []},
        ]
        api_truncated = [
            {"id": "10", "name": "Phones", "children": []},
            {"id": "20", "name": "Laptops", "children": []},
        ]
        seeds = loader._resolve_direct_child_seeds(
            dom_after_view_all,
            parent_id="1",
            api_children=api_truncated,
        )
        self.assertEqual([n["id"] for n in seeds], ["10", "20", "30", "40"])

    def test_resolve_seeds_skips_nested_dom_grandchildren(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        nested = [
            {
                "id": "10",
                "name": "Phones",
                "children": [{"id": "11", "name": "Smartphones", "children": []}],
            },
            {"id": "20", "name": "Laptops", "children": []},
        ]
        seeds = loader._resolve_direct_child_seeds(
            nested,
            parent_id="1",
            api_children=[{"id": "10", "name": "Phones"}, {"id": "20", "name": "Laptops"}],
        )
        self.assertEqual([n["id"] for n in seeds], ["10", "20"])

    def test_merge_prefers_structured_tree_over_flat_dump(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        flat = [{"id": str(i), "name": f"N{i}", "children": []} for i in range(1, 16)]
        structured = [
            {
                "id": "1",
                "name": "A",
                "children": [
                    {
                        "id": "2",
                        "name": "B",
                        "children": [{"id": "3", "name": "C", "children": []}],
                    }
                ],
            }
        ]
        merged = loader._merge_category_dict_lists(flat, structured)
        self.assertEqual(merged[0]["id"], "1")
        self.assertEqual(merged[0]["children"][0]["id"], "2")
        self.assertEqual(merged[0]["children"][0]["children"][0]["id"], "3")

    def test_complete_fills_depth_after_shallow_dom(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        nodes = loader._as_depth_seed_nodes(
            [
                {
                    "id": "10",
                    "name": "Phones",
                    "children": [{"id": "11", "name": "Smartphones", "children": []}],
                }
            ]
        )

        def fake_batch(_base, ids, _log):
            mapping = {
                "10": [{"id": "11", "name": "Smartphones", "children": []}],
                "11": [{"id": "12", "name": "Android", "children": []}],
                "12": [],
            }
            return {cid: mapping.get(cid, []) for cid in ids}

        with (
            patch.object(loader, "_fetch_categories_batch", side_effect=fake_batch),
            patch("ozon_parser.categories.human_delay"),
            patch("ozon_parser.categories.is_access_restricted", return_value=False),
            patch("ozon_parser.categories.is_blocked_page", return_value=False),
        ):
            loader._complete_category_subtree(
                "https://www.ozon.ru/category", nodes, lambda _m: None
            )

        self.assertEqual(nodes[0]["id"], "10")
        self.assertEqual(nodes[0]["children"][0]["id"], "11")
        self.assertEqual(nodes[0]["children"][0]["children"][0]["id"], "12")
        self.assertNotIn("_composer_done", nodes[0])

    def test_ensure_view_all_uses_category_filter_click(self) -> None:
        page = Mock()
        page.url = "https://www.ozon.ru/category/15500/"
        page.wait_for_timeout = Mock()
        loader = CategoryLoader(page)
        logs: list[str] = []

        with (
            patch.object(
                loader,
                "_discover_category_show_more",
                side_effect=[
                    {"present": True, "href": "", "text": "посмотреть все"},
                    {"present": False, "href": "", "text": ""},
                ],
            ),
            patch.object(loader, "_count_category_filter_links", side_effect=[5, 16]),
            patch.object(loader, "_click_category_show_more", return_value=1) as click,
            patch("ozon_parser.categories.safe_goto") as goto,
            patch("ozon_parser.categories.fast_delay"),
        ):
            result = loader._ensure_category_view_all_opened(logs.append)

        self.assertTrue(result["opened"])
        self.assertEqual(result["via"], "category_filter")
        click.assert_called()
        goto.assert_not_called()
        self.assertIn("Посмотреть все", " ".join(logs))

    def test_fetch_subtree_opens_view_all_before_dom_extract(self) -> None:
        page = Mock()
        page.url = "https://www.ozon.ru/category/10/"
        loader = CategoryLoader(page)
        order: list[str] = []

        def mark_view_all(log, on_manual_bypass=None):
            order.append("view_all")
            return {"present": True, "opened": True, "via": "category_filter", "clicks": 1}

        def mark_dom(parent_id):
            order.append("dom")
            return [{"id": "11", "name": "Child", "children": []}]

        with (
            patch("ozon_parser.categories.safe_goto", return_value=True),
            patch("ozon_parser.categories.fast_delay"),
            patch.object(loader, "_prepare_category_filter_panel"),
            patch.object(loader, "_ensure_category_view_all_opened", side_effect=mark_view_all),
            patch.object(loader, "_extract_full_subtree_dom", side_effect=mark_dom),
            patch.object(loader, "_extract_full_subtree_composer", return_value=[]),
            patch.object(loader, "_cleanup_categories", side_effect=lambda x: x),
            patch.object(loader, "_build_category_url", return_value=page.url),
        ):
            result = loader._fetch_category_subtree(
                "https://www.ozon.ru/category",
                {"id": "10", "name": "Root"},
                lambda _m: None,
                None,
            )

        self.assertEqual(order, ["view_all", "dom"])
        self.assertEqual(result[0]["id"], "11")

    def test_collect_branch_uses_composer_to_filter_flat_dom(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        root = {"id": "1", "name": "Root", "children": []}
        calls: list[str] = []

        def fake_fetch(_base, parent, _log, _bypass):
            calls.append(str(parent["id"]))
            pid = str(parent["id"])
            if pid == "1":
                # Incorrect flat dump including grandchild 11.
                return [
                    {"id": "10", "name": "Phones", "children": []},
                    {"id": "11", "name": "Smartphones", "children": []},
                    {"id": "20", "name": "Laptops", "children": []},
                ]
            if pid == "10":
                return [{"id": "11", "name": "Smartphones", "children": []}]
            return []

        def fake_batch(_base, ids, _log):
            mapping = {
                "1": [
                    {"id": "10", "name": "Phones", "children": []},
                    {"id": "20", "name": "Laptops", "children": []},
                ],
                "10": [{"id": "11", "name": "Smartphones", "children": []}],
                "20": [],
                "11": [],
            }
            # Composer knows 11 is deeper than direct under root.
            loader._composer_deeper_ids = {
                "1": {"11"},
                "10": set(),
                "20": set(),
                "11": set(),
            }
            return {cid: list(mapping.get(cid, [])) for cid in ids}

        with (
            patch.object(loader, "_fetch_category_subtree", side_effect=fake_fetch),
            patch.object(loader, "_fetch_categories_batch", side_effect=fake_batch),
            patch("ozon_parser.categories.human_delay"),
            patch("ozon_parser.categories.is_access_restricted", return_value=False),
            patch("ozon_parser.categories.is_blocked_page", return_value=False),
        ):
            loader._collect_category_branch_whole(
                "https://www.ozon.ru/category",
                root,
                log=lambda _m: None,
                on_manual_bypass=None,
                page_max_depth=15,
            )

        self.assertEqual([c["id"] for c in root["children"]], ["10", "20"])
        self.assertEqual(root["children"][0]["children"][0]["id"], "11")
        self.assertEqual(root["children"][1]["children"], [])
        # 11 is visited to confirm it is a leaf.
        self.assertEqual(calls, ["1", "10", "11", "20"])

    def test_collect_branch_switches_to_composer_after_page_depth(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        root = {"id": "10", "name": "Root", "children": []}
        calls: list[str] = []

        def fake_fetch(_base, parent, _log, _bypass):
            calls.append(str(parent["id"]))
            if str(parent["id"]) == "10":
                return [
                    {"id": "11", "name": "Audio", "children": []},
                    {"id": "12", "name": "Video", "children": []},
                ]
            return []

        def fake_batch(_base, ids, _log):
            mapping = {
                "11": [{"id": "13", "name": "Headphones", "children": []}],
                "12": [],
                "13": [{"id": "14", "name": "Wireless", "children": []}],
                "14": [],
            }
            return {cid: list(mapping.get(cid, [])) for cid in ids}

        with (
            patch.object(loader, "_fetch_category_subtree", side_effect=fake_fetch),
            patch.object(loader, "_fetch_categories_batch", side_effect=fake_batch),
            patch("ozon_parser.categories.human_delay"),
            patch("ozon_parser.categories.is_access_restricted", return_value=False),
            patch("ozon_parser.categories.is_blocked_page", return_value=False),
        ):
            loader._collect_category_branch_whole(
                "https://www.ozon.ru/category",
                root,
                log=lambda _m: None,
                on_manual_bypass=None,
                page_max_depth=0,
            )

        # Only the root page is opened; deeper levels come from Composer.
        self.assertEqual(calls, ["10"])
        self.assertEqual([c["id"] for c in root["children"]], ["11", "12"])
        self.assertEqual(root["children"][0]["children"][0]["id"], "13")
        self.assertEqual(root["children"][0]["children"][0]["children"][0]["id"], "14")
        self.assertEqual(root["children"][1]["children"], [])

    def test_collect_branch_recurses_view_all_at_every_depth(self) -> None:
        page = Mock()
        loader = CategoryLoader(page)
        root = {"id": "10", "name": "Root", "children": []}
        calls: list[str] = []

        def fake_fetch(_base, parent, _log, _bypass):
            calls.append(str(parent["id"]))
            if str(parent["id"]) == "10":
                return [
                    {"id": "11", "name": "Audio", "children": [{"id": "99", "name": "Skip"}]},
                    {"id": "12", "name": "Video", "children": []},
                ]
            if str(parent["id"]) == "11":
                return [{"id": "13", "name": "Headphones", "children": []}]
            if str(parent["id"]) == "13":
                return [{"id": "14", "name": "Wireless", "children": []}]
            return []

        with (
            patch.object(loader, "_fetch_category_subtree", side_effect=fake_fetch),
            patch.object(loader, "_fetch_categories_batch", return_value={}),
            patch("ozon_parser.categories.human_delay"),
            patch("ozon_parser.categories.is_access_restricted", return_value=False),
            patch("ozon_parser.categories.is_blocked_page", return_value=False),
        ):
            loader._collect_category_branch_whole(
                "https://www.ozon.ru/category",
                root,
                log=lambda _m: None,
                on_manual_bypass=None,
                page_max_depth=15,
            )

        # Depth-first: each level opens its own page + «Посмотреть все».
        self.assertEqual(calls, ["10", "11", "13", "14", "12"])
        self.assertEqual([c["id"] for c in root["children"]], ["11", "12"])
        self.assertEqual(root["children"][0]["children"][0]["id"], "13")
        self.assertEqual(root["children"][0]["children"][0]["children"][0]["id"], "14")
        self.assertEqual(root["children"][1]["children"], [])


if __name__ == "__main__":
    unittest.main()
