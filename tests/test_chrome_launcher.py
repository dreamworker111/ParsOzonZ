import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_parser import chrome_launcher


class ChromeLauncherTests(unittest.TestCase):
    def test_chrome_opens_all_sellers_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(
                    chrome_launcher,
                    "CHROME_OZON_PROFILE",
                    Path(temp_dir) / "profile",
                ),
                patch.object(
                    chrome_launcher,
                    "find_chrome_exe",
                    return_value=Path("chrome.exe"),
                ),
                patch.object(chrome_launcher.subprocess, "Popen") as popen,
            ):
                self.assertTrue(chrome_launcher.launch_chrome_for_ozon())

        command = popen.call_args.args[0]
        self.assertEqual(command[-1], "https://www.ozon.ru/seller/")


if __name__ == "__main__":
    unittest.main()
