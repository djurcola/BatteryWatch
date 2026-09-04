-- Durable per-file state for bounded CURRENT feed reconciliation.
-- This is additive: the archive URL migration remains the other historical
-- 009 file and is applied immediately before this file.

BEGIN;

CREATE TABLE IF NOT EXISTS current_artifact_ledger (
    feed TEXT NOT NULL CHECK (feed IN ('dispatch_scada', 'dispatch_price')),
    source_artifact_id TEXT NOT NULL CHECK (
        source_artifact_id <> ''
        AND source_artifact_id ~ '^[0-9]{1,32}$'
    ),
    source_url TEXT NOT NULL CHECK (source_url <> '' AND source_url ~ '^https://'),
    zip_filename TEXT NOT NULL CHECK (btrim(zip_filename) <> ''),
    report_timestamp TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'completed', 'missed')
    ),
    attempt_count BIGINT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_attempted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (feed, source_artifact_id),
    CHECK (
        (
            feed = 'dispatch_scada'
            AND source_url =
                'https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/'
                || zip_filename
            AND zip_filename ~
                ('^PUBLIC_DISPATCHSCADA_[0-9]{12}_' || source_artifact_id || '[.]zip$')
        )
        OR (
            feed = 'dispatch_price'
            AND source_url =
                'https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/'
                || zip_filename
            AND zip_filename ~
                ('^PUBLIC_DISPATCHIS_[0-9]{12}_' || source_artifact_id || '[.]zip$')
        )
    ),
    CHECK (date_trunc('minute', report_timestamp) = report_timestamp),
    CHECK (EXTRACT(MINUTE FROM report_timestamp)::INTEGER % 5 = 0),
    CHECK (
        last_error IS NULL
        OR (
            char_length(last_error) BETWEEN 1 AND 512
            AND last_error = btrim(last_error)
            AND last_error !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        (
            status = 'completed'
            AND completed_at IS NOT NULL
            AND last_error IS NULL
        )
        OR (
            status IN ('pending', 'missed')
            AND completed_at IS NULL
        )
    ),
    CHECK (status <> 'missed' OR last_error IS NOT NULL),
    CHECK (completed_at IS NULL OR completed_at >= first_seen_at),
    CHECK (last_attempted_at IS NULL OR last_attempted_at >= first_seen_at)
);

CREATE INDEX IF NOT EXISTS current_artifact_ledger_selection_idx
    ON current_artifact_ledger (
        feed,
        status,
        report_timestamp ASC,
        source_artifact_id ASC,
        zip_filename ASC
    );
CREATE INDEX IF NOT EXISTS current_artifact_ledger_seen_idx
    ON current_artifact_ledger (feed, last_seen_at DESC);

COMMIT;
