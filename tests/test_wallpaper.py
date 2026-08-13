import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import wallpaper


class WallpaperTests(unittest.TestCase):
    def test_parse_interval(self):
        self.assertEqual(wallpaper.parse_interval("30m"), 1800)
        self.assertEqual(wallpaper.parse_interval("6h"), 21600)
        self.assertEqual(wallpaper.parse_interval("1d"), 86400)

    def test_parse_interval_rejects_short_values(self):
        with self.assertRaises(wallpaper.WallpaperError):
            wallpaper.parse_interval("59s")

    def test_normalize_sources(self):
        self.assertEqual(
            wallpaper.normalize_sources(["aic,met", "wikimedia"]),
            ["chicago", "met", "commons"],
        )

    def test_windows_schedule(self):
        self.assertEqual(wallpaper.windows_schedule(30 * 60), ("MINUTE", 30))
        self.assertEqual(wallpaper.windows_schedule(6 * 60 * 60), ("HOURLY", 6))
        self.assertEqual(wallpaper.windows_schedule(2 * 24 * 60 * 60), ("DAILY", 2))

    def test_windows_task_uses_current_python(self):
        with mock.patch.object(wallpaper, "run_checked") as run_checked:
            wallpaper.install_windows_task({"interval_seconds": 21600}, kickstart=False)
        command = run_checked.call_args.args[0]
        self.assertEqual(command[:4], ["schtasks", "/Create", "/TN", "wallpaper"])
        self.assertIn(str(Path(wallpaper.sys.executable)), command[5])
        self.assertEqual(command[-5:], ["/SC", "HOURLY", "/MO", "6", "/F"])

    def test_image_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.png"
            Image.new("RGB", (320, 200), "navy").save(path)
            self.assertEqual(wallpaper.image_dimensions(path), (320, 200))


if __name__ == "__main__":
    unittest.main()
