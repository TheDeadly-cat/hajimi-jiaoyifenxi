from backend.config import DATABASE_PATH, HOST, PORT
from backend.instance_ownership import DatabaseInstanceOwner, InstanceAlreadyRunning
from backend.structured_logging import emit_event


def _startup_failure(phase: str, exc: BaseException) -> None:
    emit_event(
        "server_start_failed",
        severity="error",
        fields={
            "phase": phase,
            "exception_type": type(exc).__name__,
        },
    )
    raise SystemExit(1) from None


def main() -> None:
    try:
        owner = DatabaseInstanceOwner(DATABASE_PATH)
        owner.acquire(metadata={"host": HOST, "port": PORT})
    except InstanceAlreadyRunning as exc:
        _startup_failure("instance_ownership", exc)
    except Exception as exc:
        _startup_failure("instance_ownership", exc)
    try:
        # The source database remains read-only during this check.  A pending
        # schema/data migration produces a manifest-bound failure and startup
        # never invokes StudioStore._initialize against the configured file.
        from backend.database_migration import assert_database_ready_for_startup
        from backend.store import STORE

        from backend.database_startup_recovery import assert_no_pending_startup_recovery, recover_startup_database
        from pathlib import Path
        import uuid
        assert_no_pending_startup_recovery(DATABASE_PATH)
        if any(Path(str(DATABASE_PATH) + suffix).exists() for suffix in ("-wal", "-shm")):
            emit_event("startup_recovery_started", fields={"reason": "previous_session_files"})
            recover_startup_database(DATABASE_PATH,
                DATABASE_PATH.parent / "startup-recovery" / uuid.uuid4().hex,
                instance_owner=owner)
            emit_event("startup_recovery_completed")
        readiness = assert_database_ready_for_startup(DATABASE_PATH)
        STORE.configure_verified_startup(
            DATABASE_PATH,
            readiness["startup_identity"],
        )

        # Keep application import and the schema-read-only default-store open
        # behind database ownership so a second server cannot recover work.
        from backend.http_server import run_server

        run_server(instance_owner=owner)
    except Exception as exc:
        _startup_failure("database_preflight_or_host_start", exc)
    finally:
        try:
            owner.release()
        except Exception as exc:
            emit_event(
                "server_owner_release_failed",
                severity="error",
                fields={"exception_type": type(exc).__name__},
            )


if __name__ == "__main__":
    main()
