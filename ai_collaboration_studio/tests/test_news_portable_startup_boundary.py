"""The portable host must never register the legacy production entrypoint."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from backend.news_startup import NewsStartup, StartupRegistrationError


class PortableStartupBoundaryTests(unittest.TestCase):
    def test_portable_status_and_both_mutations_leave_existing_registration_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "AIStudioDailyNews.lnk"
            existing.write_bytes(b"existing synthetic registration - preserve")
            create = Mock(side_effect=AssertionError("registration must not run"))
            with patch.dict("os.environ", {"AI_STUDIO_PORTABLE_NEWS": "1"}), \
                 patch("backend.news_startup._startup_directory", side_effect=AssertionError("do not inspect existing registration")):
                startup = NewsStartup(root / "new-app", root / "new-data", startup_directory=root, create_link=create)
                status = startup.status()
                self.assertFalse(status["supported"])
                self.assertFalse(status["enabled"])
                self.assertIn("数据目录和端口", status["error"])
                for enabled in (True, False):
                    with self.assertRaises(StartupRegistrationError):
                        startup.set_enabled(enabled)
            create.assert_not_called()
            self.assertEqual(existing.read_bytes(), b"existing synthetic registration - preserve")
            self.assertEqual([p.name for p in root.iterdir()], ["AIStudioDailyNews.lnk"])

    def test_legacy_status_remains_available_without_portable_marker(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"AI_STUDIO_PORTABLE_NEWS": ""}):
            root = Path(directory)
            startup = NewsStartup(root / "app", root / "data", startup_directory=root)
            self.assertTrue(startup.status()["supported"])
            self.assertFalse(startup.status()["enabled"])


if __name__ == "__main__":
    unittest.main()
