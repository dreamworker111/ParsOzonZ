import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_parser import chrome_launcher


class ChromeLauncherTests(unittest.TestCase):
    def test_chrome_opens_blank_start_page(self):
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
        self.assertEqual(command[-1], "about:blank")
        self.assertEqual(chrome_launcher.OZON_START_URL, "https://www.ozon.ru/")

    def test_restart_chrome_kills_debug_port_instances(self):
        with patch.object(
            chrome_launcher,
            "kill_ozon_chrome_processes",
            return_value=2,
        ) as kill_proc, patch.object(
            chrome_launcher,
            "launch_chrome_for_ozon",
            return_value=True,
        ), patch.object(
            chrome_launcher,
            "wait_for_cdp",
            return_value=True,
        ), patch.object(chrome_launcher.time, "sleep"):
            self.assertTrue(chrome_launcher.restart_chrome_for_ozon())

        kill_proc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
