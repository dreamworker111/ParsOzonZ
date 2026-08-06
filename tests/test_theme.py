import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_parser import theme


class ThemePreferenceTests(unittest.TestCase):
    def test_normalize_and_default_theme(self):
        self.assertEqual(theme.normalize_theme("light"), "light")
        self.assertEqual(theme.normalize_theme("dark"), "dark")
        self.assertEqual(theme.normalize_theme("unknown"), "dark")
        self.assertEqual(theme.DEFAULT_THEME, "dark")

    def test_load_and_save_theme_preference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / "ui_settings.json"
            with (
                patch.object(theme, "SESSION_DIR", root),
                patch.object(theme, "UI_SETTINGS_PATH", settings),
            ):
                self.assertEqual(theme.load_theme_preference(), "dark")
                theme.save_theme_preference("light")
                self.assertEqual(theme.load_theme_preference(), "light")
                data = json.loads(settings.read_text(encoding="utf-8"))
                self.assertEqual(data["theme"], "light")

    def test_stylesheets_differ_for_light_and_dark(self):
        dark = theme.build_stylesheet(
            "dark",
            fs=20,
            btn_fs=14,
            cb_fs=13,
            btn_h=44,
            input_fs=14,
            tree_fs=16,
            input_h=40,
        )
        light = theme.build_stylesheet(
            "light",
            fs=20,
            btn_fs=14,
            cb_fs=13,
            btn_h=44,
            input_fs=14,
            tree_fs=16,
            input_h=40,
        )
        self.assertIn(theme.DARK.window_bg, dark)
        self.assertIn(theme.LIGHT.window_bg, light)
        self.assertNotEqual(dark, light)
        self.assertIn("ParamsScroll", dark)
        self.assertIn("QScrollBar:vertical", dark)


if __name__ == "__main__":
    unittest.main()
