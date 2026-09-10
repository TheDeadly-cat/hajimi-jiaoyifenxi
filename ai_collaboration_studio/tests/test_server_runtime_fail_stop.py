from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import server
from backend import database_migration, http_server, store as store_module


class ServerRuntimeFailStopTests(unittest.TestCase):
    def tearDown(self) -> None:
        server._FAIL_STOP_INSTANCE_OWNER = None

    def test_runtime_shutdown_failure_retains_database_owner(self) -> None:
        owner = Mock()
        fake_store = Mock()
        fake_store.configure_verified_startup = Mock()
        failure = http_server.RuntimeShutdownIncomplete("fixture stuck runtime")

        with (
            patch.object(server, "DatabaseInstanceOwner", return_value=owner),
            patch.object(server, "emit_event"),
            patch.object(
                database_migration,
                "assert_database_ready_for_startup",
                return_value={"startup_identity": {"fixture": True}},
            ),
            patch.object(store_module, "STORE", fake_store),
            patch.object(http_server, "run_server", side_effect=failure),
        ):
            with self.assertRaises(SystemExit):
                server.main()

        owner.acquire.assert_called_once()
        owner.release.assert_not_called()
        self.assertIs(server._FAIL_STOP_INSTANCE_OWNER, owner)


if __name__ == "__main__":
    unittest.main()
