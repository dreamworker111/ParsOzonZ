import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openpyxl import load_workbook
from PyQt6.QtWidgets import QApplication

from ozon_parser.app import MainWindow
from ozon_parser.config import MOBILE_MODE
from ozon_parser.export import HEADERS, ProductRow, export_products
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


class MobileLoginFlowTests(unittest.TestCase):
    def test_login_uses_headed_mobile_profile_and_marks_closed_session(self):
        page = Mock()
        page.url = "https://m.ozon.ru/"

        class FakeContext:
            reads = 0

            @property
            def pages(self):
                self.reads += 1
                return [page] if self.reads == 1 else []

        context = FakeContext()
        playwright_manager = Mock()
        playwright_manager.__enter__ = Mock(return_value=object())
        playwright_manager.__exit__ = Mock(return_value=False)

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
        ):
            self.assertTrue(MobileBrowserLoginSession().run())

        create_context.assert_called_once_with(
            playwright_manager.__enter__.return_value,
            headless=False,
            user_data_dir=profile_dir.return_value,
            browser_mode=MOBILE_MODE,
        )
        page.wait_for_timeout.assert_called_once_with(500)
        mark_saved.assert_called_once_with()


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
