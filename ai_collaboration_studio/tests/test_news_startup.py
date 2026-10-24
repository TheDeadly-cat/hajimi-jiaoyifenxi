"""Per-user startup changes tested only in temporary directories."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from backend.news_startup import NewsStartup, StartupRegistrationError
from backend.news_platform_routes import news_response


class NewsStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="news-startup-test-")
        self.root = Path(self.temp.name)
        self.app, self.data, self.startup = (self.root / name for name in ("app", "data", "startup"))
        for directory in (self.app, self.data, self.startup):
            directory.mkdir()
        (self.app / "start_daily_workspace.ps1").write_text("# isolated fixture", encoding="utf-8")
        self.created = []
        def create(destination, launcher):
            self.created.append(str(launcher))
            destination.write_text(json.dumps({"launcher": str(launcher), "background": True}), encoding="utf-8")
        self.manager = NewsStartup(self.app, self.data, startup_directory=self.startup, create_link=create)

    def tearDown(self):
        self.temp.cleanup()

    def test_reading_status_does_not_register_or_create_files(self):
        self.assertFalse(self.manager.status()["enabled"])
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertEqual(list(self.startup.iterdir()), [])

    def test_enable_idempotence_and_disable_archives_owned_entry(self):
        self.assertTrue(self.manager.set_enabled(True)["enabled"])
        original = self.manager.entry.read_bytes()
        self.assertTrue(self.manager.set_enabled(True)["enabled"])
        self.assertEqual(len(self.created), 1)
        self.assertFalse(self.manager.set_enabled(False)["enabled"])
        self.assertFalse(self.manager.entry.exists())
        copies = list(self.manager.history.rglob("previous-entry.lnk"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), original)
        self.assertFalse(self.manager.set_enabled(False)["enabled"])

    def test_unrecognized_or_changed_entry_is_never_overwritten(self):
        self.manager.entry.write_bytes(b"USER OWNED ENTRY")
        self.assertTrue(self.manager.status()["error"])
        for enabled in (False, True):
            with self.assertRaises(StartupRegistrationError):
                self.manager.set_enabled(enabled)
            self.assertEqual(self.manager.entry.read_bytes(), b"USER OWNED ENTRY")

    def test_tampering_after_success_is_reported_without_deleting(self):
        self.manager.set_enabled(True)
        self.manager.entry.write_bytes(b"changed")
        self.assertFalse(self.manager.status()["enabled"])
        with self.assertRaises(StartupRegistrationError):
            self.manager.set_enabled(False)
        self.assertEqual(self.manager.entry.read_bytes(), b"changed")

    def test_failed_version_update_restores_exact_old_entry(self):
        self.manager.set_enabled(True)
        old_entry, old_record = self.manager.entry.read_bytes(), self.manager.record.read_bytes()
        app2 = self.root / "app2"; app2.mkdir()
        (app2 / "start_daily_workspace.ps1").write_text("# new fixture", encoding="utf-8")
        def fail(*_):
            raise OSError("synthetic creation failure")
        manager2 = NewsStartup(app2, self.data, startup_directory=self.startup, create_link=fail)
        with self.assertRaises(StartupRegistrationError):
            manager2.set_enabled(True)
        self.assertEqual(self.manager.entry.read_bytes(), old_entry)
        self.assertEqual(self.manager.record.read_bytes(), old_record)
        self.assertTrue(self.manager.status()["enabled"])

    def test_new_version_rebinds_only_recognized_registration(self):
        self.manager.set_enabled(True)
        app2 = self.root / "app2"; app2.mkdir()
        (app2 / "start_daily_workspace.ps1").write_text("# new fixture", encoding="utf-8")
        manager2 = NewsStartup(app2, self.data, startup_directory=self.startup, create_link=self.manager._create_link)
        self.assertFalse(manager2.status()["version_current"])
        self.assertTrue(manager2.set_enabled(True)["version_current"])
        self.assertEqual(json.loads(manager2.record.read_text(encoding="utf-8"))["app"], str(app2))

    def test_route_rejects_non_boolean_extra_fields_and_query(self):
        class Runtime:
            startup = self.manager
        for payload in ({"enabled": 1}, {}, {"enabled": True, "path": "elsewhere"}):
            self.assertEqual(news_response(Runtime(), "/api/news/startup", payload=payload, method="POST")[1], 400)
        self.assertEqual(news_response(Runtime(), "/api/news/startup", payload={"enabled": True}, query={"extra": ["1"]}, method="POST")[1], 400)
        self.assertEqual(self.created, [])
        response, code = news_response(Runtime(), "/api/news/startup", payload={"enabled": True}, method="POST")
        self.assertEqual(code, 200)
        self.assertTrue(response["startup"]["enabled"])

    def test_unavailable_startup_folder_does_not_abort_optional_manager(self):
        with mock.patch("backend.news_startup._startup_directory", side_effect=OSError("synthetic unavailable folder")):
            manager = NewsStartup(self.app, self.data, create_link=self.manager._create_link)
        self.assertFalse(manager.status()["supported"])
        self.assertTrue(manager.status()["error"])
        with self.assertRaises(StartupRegistrationError):
            manager.set_enabled(True)
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertEqual(list(self.startup.iterdir()), [])

    def test_rejected_path_does_not_abort_optional_manager(self):
        with mock.patch("backend.news_startup._safe", side_effect=StartupRegistrationError("synthetic path alias")):
            manager = NewsStartup(self.app, self.data, startup_directory=self.startup)
        self.assertFalse(manager.status()["supported"])
        with self.assertRaises(StartupRegistrationError):
            manager.set_enabled(False)

    def _second_version(self):
        app2 = self.root / "app2"
        app2.mkdir()
        (app2 / "start_daily_workspace.ps1").write_text("# new fixture", encoding="utf-8")
        return NewsStartup(app2, self.data, startup_directory=self.startup, create_link=self.manager._create_link)

    def test_first_enable_interrupted_after_entry_before_record_recovers(self):
        with mock.patch.object(self.manager, "_replace_record", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.set_enabled(True)
        self.assertTrue(self.manager.entry.exists())
        self.assertFalse(self.manager.record.exists())
        pending = self.manager.pending.read_bytes()
        self.assertTrue(self.manager.status()["pending"])
        self.assertEqual(self.manager.pending.read_bytes(), pending)
        restored = NewsStartup(self.app, self.data, startup_directory=self.startup, create_link=self.manager._create_link)
        self.assertTrue(restored.set_enabled(True)["enabled"])
        self.assertFalse(restored.pending.exists())
        self.assertEqual(len(self.created), 1)

    def test_version_change_interrupted_before_old_entry_move_recovers(self):
        self.manager.set_enabled(True)
        old_entry = self.manager.entry.read_bytes()
        manager = self._second_version()
        with mock.patch.object(manager, "_apply_transaction", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                manager.set_enabled(True)
        self.assertEqual(manager.entry.read_bytes(), old_entry)
        self.assertTrue(manager.status()["pending"])
        self.assertTrue(manager.set_enabled(True)["version_current"])
        self.assertEqual(len(self.created), 2)
        self.assertEqual(next(manager.history.rglob("previous-entry.lnk")).read_bytes(), old_entry)

    def test_version_change_interrupted_after_publication_recovers(self):
        self.manager.set_enabled(True)
        old_record = self.manager.record.read_bytes()
        manager = self._second_version()
        with mock.patch.object(manager, "_replace_record", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                manager.set_enabled(True)
        self.assertEqual(manager.record.read_bytes(), old_record)
        self.assertTrue(manager.set_enabled(True)["version_current"])
        self.assertFalse(manager.pending.exists())

    def test_disable_interrupted_after_archiving_recovers_without_new_link(self):
        self.manager.set_enabled(True)
        old_entry = self.manager.entry.read_bytes()
        with mock.patch.object(self.manager, "_replace_record", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.set_enabled(False)
        self.assertFalse(self.manager.entry.exists())
        self.assertTrue(self.manager.status()["pending"])
        self.assertFalse(self.manager.set_enabled(False)["enabled"])
        self.assertEqual(len(self.created), 1)
        self.assertEqual(next(self.manager.history.rglob("previous-entry.lnk")).read_bytes(), old_entry)

    def test_pending_recovery_never_moves_foreign_replacement(self):
        with mock.patch.object(self.manager, "_replace_record", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.set_enabled(True)
        self.manager.entry.write_bytes(b"USER REPLACEMENT AFTER INTERRUPTION")
        pending = self.manager.pending.read_bytes()
        for enabled in (True, False):
            with self.assertRaises(StartupRegistrationError):
                self.manager.set_enabled(enabled)
            self.assertEqual(self.manager.entry.read_bytes(), b"USER REPLACEMENT AFTER INTERRUPTION")
            self.assertEqual(self.manager.pending.read_bytes(), pending)
            self.assertFalse(self.manager.record.exists())

    def test_record_publish_failure_rolls_back_exact_old_version(self):
        self.manager.set_enabled(True)
        old_entry, old_record = self.manager.entry.read_bytes(), self.manager.record.read_bytes()
        manager = self._second_version()
        with mock.patch.object(manager, "_replace_record", side_effect=OSError("synthetic record failure")):
            with self.assertRaises(StartupRegistrationError):
                manager.set_enabled(True)
        self.assertEqual(manager.entry.read_bytes(), old_entry)
        self.assertEqual(manager.record.read_bytes(), old_record)
        self.assertFalse(manager.pending.exists())
        self.assertTrue(self.manager.status()["enabled"])

    def test_committed_record_before_transaction_close_is_recoverable(self):
        import os
        rename = os.rename
        def interrupt_close(source, target):
            if Path(source) == self.manager.pending:
                raise KeyboardInterrupt
            return rename(source, target)
        with mock.patch("backend.news_startup.os.rename", side_effect=interrupt_close):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.set_enabled(True)
        entry, record = self.manager.entry.read_bytes(), self.manager.record.read_bytes()
        self.assertTrue(self.manager.set_enabled(True)["enabled"])
        self.assertEqual(self.manager.entry.read_bytes(), entry)
        self.assertEqual(self.manager.record.read_bytes(), record)
        self.assertFalse(self.manager.pending.exists())

    def test_version_update_recovers_after_old_entry_move_before_new_publication(self):
        import os
        self.manager.set_enabled(True)
        manager = self._second_version()
        rename = os.rename
        def interrupt_publication(source, target):
            if Path(source).name == "new-entry.lnk":
                raise KeyboardInterrupt
            return rename(source, target)
        with mock.patch("backend.news_startup.os.rename", side_effect=interrupt_publication):
            with self.assertRaises(KeyboardInterrupt):
                manager.set_enabled(True)
        self.assertFalse(manager.entry.exists())
        self.assertTrue(manager.set_enabled(True)["version_current"])
        self.assertFalse(manager.pending.exists())

    def test_interrupted_rollback_resumes_before_applying_next_setting(self):
        import os
        self.manager.set_enabled(True)
        old_entry, old_record = self.manager.entry.read_bytes(), self.manager.record.read_bytes()
        manager = self._second_version()
        rename = os.rename
        def interrupt_rollback(source, target):
            result = rename(source, target)
            if Path(target).name == "failed-entry.lnk":
                raise KeyboardInterrupt
            return result
        with mock.patch.object(manager, "_replace_record", side_effect=OSError("synthetic write failure")):
            with mock.patch("backend.news_startup.os.rename", side_effect=interrupt_rollback):
                with self.assertRaises(KeyboardInterrupt):
                    manager.set_enabled(True)
        self.assertTrue(manager.status()["pending"])
        self.assertTrue(self.manager.set_enabled(True)["version_current"])
        self.assertEqual(self.manager.entry.read_bytes(), old_entry)
        self.assertEqual(self.manager.record.read_bytes(), old_record)
        self.assertFalse(manager.pending.exists())

    def test_pending_recovery_does_not_replace_foreign_registration(self):
        with mock.patch.object(self.manager, "_replace_record", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.set_enabled(True)
        self.manager.record.write_bytes(b"USER OWNED REGISTRATION")
        entry, pending = self.manager.entry.read_bytes(), self.manager.pending.read_bytes()
        for enabled in (True, False):
            with self.assertRaises(StartupRegistrationError):
                self.manager.set_enabled(enabled)
            self.assertEqual(self.manager.entry.read_bytes(), entry)
            self.assertEqual(self.manager.record.read_bytes(), b"USER OWNED REGISTRATION")
            self.assertEqual(self.manager.pending.read_bytes(), pending)


if __name__ == "__main__":
    unittest.main()
