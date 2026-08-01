import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openpyxl import load_workbook
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from ozon_parser.app import FilterTreeWidget, MainWindow, scaled_font_size
from ozon_parser.config import MOBILE_MODE
from ozon_parser.export import HEADERS, ProductRow, export_products
from ozon_parser.filters import FilterOptionNode
from ozon_parser.login import MobileBrowserLoginSession


class GuiModeSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mobile_auth_controls_and_missing_session_guard(self):
        window = MainWindow()
        window.show()
        self.app.processEvents()
        try:
            self.assertTrue(window.chrome_btn.isVisible())
            self.assertFalse(window.mobile_login_btn.isVisible())

            window.browser_mode_combo.setCurrentIndex(
                window.browser_mode_combo.findData(MOBILE_MODE)
            )
            window.auth_mode_combo.setCurrentIndex(
                window.auth_mode_combo.findData(True)
            )
            self.app.processEvents()

            self.assertFalse(window.chrome_btn.isVisible())
            self.assertTrue(window.mobile_login_btn.isVisible())
            with (
                patch(
                    "ozon_parser.app.has_mobile_saved_session",
                    return_value=False,
                ),
                patch("ozon_parser.app.QMessageBox.warning") as warning,
            ):
                self.assertFalse(window._validate_mobile_auth())
            warning.assert_called_once()
            self.assertIn("завершите вход", warning.call_args.args[2].lower())
        finally:
            window.close()

    def test_compact_screen_font_scaling_has_no_threshold_jump(self):
        self.assertLessEqual(
            scaled_font_size(800) - scaled_font_size(799),
            1,
        )
        self.assertEqual(scaled_font_size(1080), 27)

    def test_category_can_be_renamed_without_changing_ozon_target(self):
        tree = FilterTreeWidget()
        child = FilterOptionNode(
            id="child-id",
            name="Старое имя",
            url="https://www.ozon.ru/category/123/",
            param_key="category",
            param_value="123",
            category_id="123",
            category_name="Старое имя",
            parent_name="Родитель",
        )
        tree.populate_categories([child])
        item = tree.topLevelItem(0)

        self.assertTrue(tree.rename_category_item(item, "  Новое   имя  "))
        item.setCheckState(0, Qt.CheckState.Checked)
        target = tree.selected_leaf_categories()[0]

        self.assertEqual(item.text(0), "Новое имя")
        self.assertEqual(target.name, "Родитель → Новое имя")
        self.assertEqual(target.category_name, "Новое имя")
        self.assertEqual(target.id, "child-id")
        self.assertEqual(target.category_id, "123")
        self.assertEqual(target.url, "https://www.ozon.ru/category/123/")

    def test_specific_seller_checkbox_controls_link_input(self):
        window = MainWindow()
        try:
            self.assertFalse(window.specific_seller_checkbox.isChecked())
            self.assertFalse(window.seller_input.isEnabled())
            self.assertEqual(window.seller_input.text(), "")

            window.specific_seller_checkbox.setChecked(True)
            self.app.processEvents()
            self.assertTrue(window.seller_input.isEnabled())
            self.assertIn("/seller/", window.seller_input.placeholderText())

            window.specific_seller_checkbox.setChecked(False)
            self.app.processEvents()
            self.assertFalse(window.seller_input.isEnabled())
            self.assertIn("всех продавцов", window.seller_input.placeholderText())
        finally:
            window.close()


class MobileLoginFlowTests(unittest.TestCase):
    def test_login_verifies_session_after_manual_confirmation(self):
        page = Mock()
        page.url = "https://m.ozon.ru/"
        page.evaluate.return_value = ""

        class FakeContext:
            ready = False

            @property
            def pages(self):
                return [page]

            def cookies(self):
                if not self.ready:
                    return []
                return [
                    {
                        "name": "__Secure-access-token",
                        "value": "present",
                        "domain": ".ozon.ru",
                        "expires": -1,
                    },
                    {
                        "name": "__Secure-user-id",
                        "value": "12345",
                        "domain": ".ozon.ru",
                        "expires": -1,
                    },
                ]

            def close(self):
                pass

        context = FakeContext()
        playwright_manager = Mock()
        playwright_manager.__enter__ = Mock(return_value=object())
        playwright_manager.__exit__ = Mock(return_value=False)

        def confirm() -> bool:
            context.ready = True
            page.url = "https://m.ozon.ru/my/main"
            page.evaluate.return_value = "Выйти Мои заказы"
            return True

        with (
            patch(
                "ozon_parser.login.sync_playwright",
                return_value=playwright_manager,
            ),
            patch(
                "ozon_parser.login.create_persistent_context",
                return_value=(context, page),
            ) as create_context,
            patch("ozon_parser.login.mobile_browser_profile_dir") as profile_dir,
            patch("ozon_parser.login.mark_mobile_session_saved") as mark_saved,
            patch("ozon_parser.login.settle_mobile_login"),
        ):
            self.assertTrue(
                MobileBrowserLoginSession().run(
                    wait_for_confirmation=confirm,
                )
            )

        create_context.assert_called_once_with(
            playwright_manager.__enter__.return_value,
            headless=False,
            user_data_dir=profile_dir.return_value,
            browser_mode=MOBILE_MODE,
        )
        mark_saved.assert_called_once_with()

    def test_login_keeps_asking_until_cancelled_without_session(self):
        page = Mock()
        page.url = "https://m.ozon.ru/"
        page.evaluate.return_value = "Войти или зарегистрироваться"
        context = Mock()
        context.pages = [page]
        context.cookies.return_value = []
        playwright_manager = Mock()
        playwright_manager.__enter__ = Mock(return_value=object())
        playwright_manager.__exit__ = Mock(return_value=False)

        confirms = {"n": 0}

        def wait_for_confirmation() -> bool:
            confirms["n"] += 1
            return confirms["n"] < 3

        with (
            patch(
                "ozon_parser.login.sync_playwright",
                return_value=playwright_manager,
            ),
            patch(
                "ozon_parser.login.create_persistent_context",
                return_value=(context, page),
            ),
            patch("ozon_parser.login.mark_mobile_session_saved") as mark_saved,
            patch("ozon_parser.login.settle_mobile_login"),
            patch("ozon_parser.login._wait_until_authenticated", return_value=False),
        ):
            self.assertFalse(
                MobileBrowserLoginSession().run(
                    wait_for_confirmation=wait_for_confirmation,
                )
            )

        self.assertEqual(confirms["n"], 3)
        mark_saved.assert_not_called()


class XlsxRegressionTests(unittest.TestCase):
    def test_export_contains_all_seven_populated_product_fields(self):
        products = [
            ProductRow(
                name="Товар 2",
                price_discounted=200,
                price_original=250,
                url="https://www.ozon.ru/product/item-2/",
                bonus_points=20,
            ),
            ProductRow(
                name="Товар 1",
                price_discounted=100,
                price_original=125,
                url="https://www.ozon.ru/product/item-1/",
                bonus_points=10,
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = export_products(products, output_dir=Path(temp_dir))
            workbook = load_workbook(path, data_only=False)
            sheet = workbook["Товары"]

            self.assertEqual(
                [sheet.cell(1, column).value for column in range(1, 8)],
                list(HEADERS),
            )
            self.assertEqual(sheet.max_row, 3)
            for row in range(2, 4):
                values = [
                    sheet.cell(row, column).value
                    for column in range(1, 8)
                ]
                self.assertTrue(all(value is not None for value in values))
                self.assertTrue(sheet.cell(row, 6).hyperlink.target)
            self.assertEqual(sheet.cell(2, 1).value, "Товар 1")


if __name__ == "__main__":
    unittest.main()
