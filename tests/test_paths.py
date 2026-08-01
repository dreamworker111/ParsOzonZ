import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_parser import paths


class PathsTests(unittest.TestCase):
    def test_source_paths_use_project_root(self):
        root = paths.writable_root()
        self.assertTrue((root / "main.py").exists() or (root / "ozon_parser").exists())
        self.assertEqual(paths.resource_root(), root)

    def test_frozen_paths_split_resources_and_writable(self):
        meipass = Path("C:/fake/_MEIPASS")
        exe = Path("C:/Apps/OzonParser.exe")
        with (
            patch.object(paths.sys, "frozen", True, create=True),
            patch.object(paths.sys, "_MEIPASS", str(meipass), create=True),
            patch.object(paths.sys, "executable", str(exe)),
        ):
            self.assertTrue(paths.is_frozen())
            self.assertEqual(paths.resource_root(), meipass)
            self.assertEqual(paths.writable_root(), exe.parent)


if __name__ == "__main__":
    unittest.main()
