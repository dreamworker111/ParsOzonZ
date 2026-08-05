"""Tests for staged root → selected-subtree category loading."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ozon_parser.categories import CategoryTarget
from ozon_parser.config import DESKTOP_MODE
from ozon_parser.filters import FilterOptionNode
from ozon_parser.parser import OzonParser


class StagedCategoryLoadTests(unittest.TestCase):
    def test_roots_only_uses_global_root_tree(self) -> None:
        roots = [
            FilterOptionNode(
                id="15500",
                name="Электроника",
                param_key="category",
                param_value="15500",
                category_id="15500",
            )
        ]
        parser = OzonParser()
        loader = Mock()
        loader.load_global_root_tree.return_value = roots

        with (
            patch("ozon_parser.parser.sync_playwright") as sync_pw,
            patch.object(parser, "_open_browser", return_value=(None, Mock(), Mock(), "cdp")),
            patch("ozon_parser.parser.ensure_ozon_session_ready", return_value=True),
            patch("ozon_parser.parser.CategoryLoader", return_value=loader),
            patch("ozon_parser.parser.close_session_context"),
        ):
            sync_pw.return_value.__enter__.return_value = Mock()
            result = parser.load_categories(
                "",
                specific_seller=False,
                roots_only=True,
                prefer_cache=False,
            )

        self.assertEqual(result, roots)
        loader.load_global_root_tree.assert_called_once()
        loader.load_global_category_tree.assert_not_called()

    def test_expand_selected_global_branches(self) -> None:
        targets = [
            CategoryTarget(
                id="Категория|category:15500",
                name="Электроника",
                param_key="category",
                param_value="15500",
                category_id="15500",
            )
        ]
        branch = FilterOptionNode(
            id="15500",
            name="Электроника",
            param_key="category",
            param_value="15500",
            category_id="15500",
            children=[
                FilterOptionNode(
                    id="15501",
                    name="Телефоны",
                    param_key="category",
                    param_value="15501",
                    category_id="15501",
                    parent_name="Электроника",
                )
            ],
        )
        parser = OzonParser()
        loader = Mock()
        loader.expand_global_category_subtrees.return_value = [branch]

        with (
            patch("ozon_parser.parser.sync_playwright") as sync_pw,
            patch.object(parser, "_open_browser", return_value=(None, Mock(), Mock(), "cdp")),
            patch("ozon_parser.parser.ensure_ozon_session_ready", return_value=True),
            patch("ozon_parser.parser.CategoryLoader", return_value=loader),
            patch("ozon_parser.parser.close_session_context"),
        ):
            sync_pw.return_value.__enter__.return_value = Mock()
            result = parser.expand_selected_category_subtrees(
                targets,
                specific_seller=False,
                browser_mode=DESKTOP_MODE,
            )

        self.assertEqual(result, [branch])
        kwargs = loader.expand_global_category_subtrees.call_args
        self.assertEqual(kwargs.args[0], ["15500"])
        self.assertIn("15500", kwargs.kwargs.get("root_meta") or kwargs[1].get("root_meta", {}))


if __name__ == "__main__":
    unittest.main()
