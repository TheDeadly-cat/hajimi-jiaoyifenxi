from backend.config import DATABASE_PATH, HOST, PORT
from backend.instance_ownership import DatabaseInstanceOwner, InstanceAlreadyRunning


def main() -> None:
    owner = DatabaseInstanceOwner(DATABASE_PATH)
    try:
        owner.acquire(metadata={"host": HOST, "port": PORT})
    except InstanceAlreadyRunning as exc:
        raise SystemExit(str(exc)) from exc
    try:
        # The source database remains read-only during this check.  A pending
        # schema/data migration produces a manifest-bound failure and startup
        # never invokes StudioStore._initialize against the configured file.
        from backend.database_migration import (
            DatabaseMigrationError,
            assert_database_ready_for_startup,
        )
        from backend.store import STORE

        try:
            readiness = assert_database_ready_for_startup(DATABASE_PATH)
        except DatabaseMigrationError as exc:
            raise SystemExit(str(exc)) from exc
        STORE.configure_verified_startup(
            DATABASE_PATH,
            readiness["startup_identity"],
        )

        # Keep application import and the schema-read-only default-store open
        # behind database ownership so a second server cannot recover work.
        from backend.http_server import run_server

        run_server(instance_owner=owner)
    finally:
        owner.release()


if __name__ == "__main__":
    main()
