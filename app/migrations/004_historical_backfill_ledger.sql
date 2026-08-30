-- Resumable operator-controlled historical backfill run state and event history.

CREATE TABLE IF NOT EXISTS historical_backfill_runs (
    run_id TEXT PRIMARY KEY CHECK (
        length(run_id) BETWEEN 1 AND 64
        AND run_id ~ '^[A-Za-z0-9._-]+$'
    ),
    requested_start TIMESTAMPTZ NOT NULL,
    requested_end TIMESTAMPTZ NOT NULL,
    ingestion_version BIGINT NOT NULL CHECK (ingestion_version >= 0),
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'completed', 'partial', 'failed')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CHECK (requested_end > requested_start),
    CHECK (requested_end - requested_start <= INTERVAL '366 days')
);

CREATE TABLE IF NOT EXISTS historical_backfill_items (
    run_id TEXT NOT NULL,
    feed TEXT NOT NULL CHECK (feed IN ('dispatch_scada', 'dispatch_price')),
    report_date DATE NOT NULL,
    source_url TEXT NOT NULL CHECK (
        source_url ~ '^https://www[.]nemweb[.]com[.]au/REPORTS/ARCHIVE/'
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'completed', 'failed')
    ),
    attempt_count BIGINT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, feed, report_date),
    CONSTRAINT historical_backfill_items_run_fk
        FOREIGN KEY (run_id)
        REFERENCES historical_backfill_runs (run_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS historical_backfill_events (
    event_seq BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    feed TEXT NOT NULL CHECK (feed IN ('dispatch_scada', 'dispatch_price')),
    report_date DATE NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('planned', 'recovered', 'claimed', 'completed', 'failed')
    ),
    attempt_number BIGINT NOT NULL DEFAULT 0 CHECK (attempt_number >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT historical_backfill_events_item_fk
        FOREIGN KEY (run_id, feed, report_date)
        REFERENCES historical_backfill_items (run_id, feed, report_date)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS historical_backfill_runs_status_idx
    ON historical_backfill_runs (status, updated_at, run_id);

CREATE INDEX IF NOT EXISTS historical_backfill_items_claim_idx
    ON historical_backfill_items (run_id, status, feed, report_date);

CREATE INDEX IF NOT EXISTS historical_backfill_events_order_idx
    ON historical_backfill_events (run_id, event_seq);
