"""DDL installed only through StudioStore's owned initialization/migration path."""


def ensure_news_review_schema(connection, *, applied_at_ms):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS news_review_policies (
            id TEXT PRIMARY KEY, policy_json TEXT NOT NULL, policy_sha256 TEXT NOT NULL UNIQUE,
            provider_run_id TEXT NOT NULL REFERENCES provider_execution_runs(id), approved_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','PAUSED','STOPPED')),
            stop_reason TEXT NOT NULL DEFAULT '', cursor INTEGER NOT NULL,
            documents_reserved INTEGER NOT NULL DEFAULT 0 CHECK(documents_reserved>=0),
            calls_reserved INTEGER NOT NULL DEFAULT 0 CHECK(calls_reserved>=0),
            tokens_reserved INTEGER NOT NULL DEFAULT 0 CHECK(tokens_reserved>=0),
            cost_reserved TEXT NOT NULL DEFAULT '0'
        );
        CREATE TRIGGER IF NOT EXISTS news_review_policy_identity_immutable
        BEFORE UPDATE OF id,policy_json,policy_sha256,provider_run_id,approved_at ON news_review_policies
        BEGIN SELECT RAISE(ABORT,'news review policy identity is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS news_review_policy_no_delete BEFORE DELETE ON news_review_policies
        BEGIN SELECT RAISE(ABORT,'news review policy is permanent'); END;
        CREATE TRIGGER IF NOT EXISTS news_review_policy_no_refund
        BEFORE UPDATE ON news_review_policies
        WHEN NEW.documents_reserved<OLD.documents_reserved OR NEW.calls_reserved<OLD.calls_reserved
             OR NEW.tokens_reserved<OLD.tokens_reserved OR NEW.cursor<OLD.cursor
        BEGIN SELECT RAISE(ABORT,'news review reservations cannot be refunded'); END;
        CREATE TABLE IF NOT EXISTS news_review_events (
            policy_id TEXT NOT NULL REFERENCES news_review_policies(id),
            item_id TEXT NOT NULL REFERENCES source_inbox_items(id),
            event_key TEXT NOT NULL, discovered_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'WAITING_DOCUMENT', error_code TEXT NOT NULL DEFAULT '',
            document_job_id TEXT NOT NULL DEFAULT '', review_job_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(policy_id,item_id)
        );
        CREATE TABLE IF NOT EXISTS news_review_jobs (
            id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
            policy_id TEXT NOT NULL REFERENCES news_review_policies(id),
            item_id TEXT NOT NULL REFERENCES source_inbox_items(id),
            document_version_id TEXT NOT NULL REFERENCES source_document_versions(id),
            strategy_sha256 TEXT NOT NULL, input_json TEXT NOT NULL, input_sha256 TEXT NOT NULL,
            request_sha256 TEXT NOT NULL, request_bytes INTEGER NOT NULL,
            tokens_reserved INTEGER NOT NULL, cost_reserved TEXT NOT NULL,
            created_at INTEGER NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'QUEUED'
                CHECK(status IN ('QUEUED','RUNNING','REVIEWED','MATERIAL_INSUFFICIENT','FAILED','UNKNOWN','CANCELLED')),
            attempt_id TEXT NOT NULL DEFAULT '', started_at INTEGER NOT NULL DEFAULT 0,
            finished_at INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT '',
            http_attempted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS news_review_jobs_queue ON news_review_jobs(status,created_at);
        CREATE TRIGGER IF NOT EXISTS news_review_job_identity_immutable
        BEFORE UPDATE OF id,dedupe_key,policy_id,item_id,document_version_id,strategy_sha256,
            input_json,input_sha256,request_sha256,request_bytes,tokens_reserved,cost_reserved,created_at,priority ON news_review_jobs
        BEGIN SELECT RAISE(ABORT,'news review input is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS news_review_job_no_delete BEFORE DELETE ON news_review_jobs
        BEGIN SELECT RAISE(ABORT,'news review job is permanent'); END;
        CREATE TRIGGER IF NOT EXISTS news_review_job_forward_only BEFORE UPDATE ON news_review_jobs
        WHEN (OLD.status!=NEW.status AND NOT (
                (OLD.status='QUEUED' AND NEW.status IN ('RUNNING','CANCELLED')) OR
                (OLD.status='RUNNING' AND NEW.status IN ('REVIEWED','MATERIAL_INSUFFICIENT','FAILED','UNKNOWN','CANCELLED'))))
             OR (OLD.attempt_id!='' AND NEW.attempt_id!=OLD.attempt_id)
             OR (OLD.started_at!=0 AND NEW.started_at!=OLD.started_at)
             OR NEW.http_attempted<OLD.http_attempted
        BEGIN SELECT RAISE(ABORT,'news review jobs cannot be replayed'); END;
        CREATE TABLE IF NOT EXISTS news_review_receipts (
            job_id TEXT PRIMARY KEY REFERENCES news_review_jobs(id), receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS news_review_receipt_no_update BEFORE UPDATE ON news_review_receipts
        BEGIN SELECT RAISE(ABORT,'news review receipt is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS news_review_receipt_no_delete BEFORE DELETE ON news_review_receipts
        BEGIN SELECT RAISE(ABORT,'news review receipt is permanent'); END;
        CREATE TABLE IF NOT EXISTS news_review_links (
            policy_id TEXT NOT NULL REFERENCES news_review_policies(id),
            item_id TEXT NOT NULL REFERENCES source_inbox_items(id),
            job_id TEXT NOT NULL REFERENCES news_review_jobs(id), linked_at INTEGER NOT NULL,
            PRIMARY KEY(policy_id,item_id,job_id)
        );
        CREATE TRIGGER IF NOT EXISTS news_review_link_no_update BEFORE UPDATE ON news_review_links
        BEGIN SELECT RAISE(ABORT,'news review link is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS news_review_link_no_delete BEFORE DELETE ON news_review_links
        BEGIN SELECT RAISE(ABORT,'news review link is permanent'); END;
        CREATE TABLE IF NOT EXISTS news_review_observations (
            policy_id TEXT NOT NULL REFERENCES news_review_policies(id), sequence INTEGER NOT NULL,
            observation_json TEXT NOT NULL, observation_sha256 TEXT NOT NULL,
            PRIMARY KEY(policy_id,sequence)
        );
        CREATE TRIGGER IF NOT EXISTS news_review_observation_no_update BEFORE UPDATE ON news_review_observations
        BEGIN SELECT RAISE(ABORT,'news review observation is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS news_review_observation_no_delete BEFORE DELETE ON news_review_observations
        BEGIN SELECT RAISE(ABORT,'news review observation is permanent'); END;
        CREATE TABLE IF NOT EXISTS news_review_source_grants (
            policy_id TEXT NOT NULL REFERENCES news_review_policies(id), adapter_key TEXT NOT NULL,
            grant_json TEXT NOT NULL, grant_sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PREPARED' CHECK(status IN ('PREPARED','APPLIED','WITHDRAWN')),
            PRIMARY KEY(policy_id,adapter_key)
        );
        CREATE TRIGGER IF NOT EXISTS news_review_source_grant_immutable
        BEFORE UPDATE OF policy_id,adapter_key,grant_json,grant_sha256 ON news_review_source_grants
        BEGIN SELECT RAISE(ABORT,'news source grant is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS news_review_source_grant_no_delete BEFORE DELETE ON news_review_source_grants
        BEGIN SELECT RAISE(ABORT,'news source grant is permanent'); END;
    """)
    connection.execute("INSERT OR IGNORE INTO schema_migrations(key,applied_at) VALUES(?,?)",
                       ("news_event_review_v1", applied_at_ms))
