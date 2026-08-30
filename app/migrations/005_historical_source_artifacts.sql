-- Immutable official daily archive bytes and their historical backfill attempts.

CREATE TABLE IF NOT EXISTS historical_source_artifacts (
    artifact_sha256 TEXT PRIMARY KEY CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    feed TEXT NOT NULL CHECK (feed IN ('dispatch_price', 'dispatch_scada')),
    report_date DATE NOT NULL,
    source_url TEXT NOT NULL,
    filename TEXT NOT NULL,
    byte_count BIGINT NOT NULL CHECK (byte_count > 0),
    raw_bytes BYTEA NOT NULL CHECK (octet_length(raw_bytes) > 0),
    stored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT historical_source_artifacts_archive_identity_ck CHECK (
        (
            feed = 'dispatch_price'
            AND source_url =
                'https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/' || filename
            AND filename = 'PUBLIC_DISPATCHIS_' || to_char(report_date, 'YYYYMMDD') || '.zip'
        )
        OR (
            feed = 'dispatch_scada'
            AND source_url =
                'https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/' || filename
            AND filename = 'PUBLIC_DISPATCHSCADA_' || to_char(report_date, 'YYYYMMDD') || '.zip'
        )
    ),
    CONSTRAINT historical_source_artifacts_byte_count_ck CHECK (
        byte_count = octet_length(raw_bytes)
    ),
    CONSTRAINT historical_source_artifacts_link_target_uq
        UNIQUE (artifact_sha256, feed, report_date)
);

CREATE TABLE IF NOT EXISTS historical_backfill_item_artifacts (
    run_id TEXT NOT NULL,
    feed TEXT NOT NULL CHECK (feed IN ('dispatch_price', 'dispatch_scada')),
    report_date DATE NOT NULL,
    attempt_number BIGINT NOT NULL CHECK (attempt_number > 0),
    artifact_sha256 TEXT NOT NULL,
    downloaded_at TIMESTAMPTZ NOT NULL,
    source_last_modified TIMESTAMPTZ,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, feed, report_date, attempt_number),
    CONSTRAINT historical_backfill_item_artifacts_item_fk
        FOREIGN KEY (run_id, feed, report_date)
        REFERENCES historical_backfill_items (run_id, feed, report_date)
        ON DELETE RESTRICT,
    CONSTRAINT historical_backfill_item_artifacts_source_fk
        FOREIGN KEY (artifact_sha256, feed, report_date)
        REFERENCES historical_source_artifacts (artifact_sha256, feed, report_date)
        ON DELETE RESTRICT,
    CONSTRAINT historical_backfill_item_artifacts_last_modified_ck CHECK (
        source_last_modified IS NULL OR source_last_modified <= downloaded_at
    )
);

CREATE INDEX IF NOT EXISTS historical_source_artifacts_feed_report_date_idx
    ON historical_source_artifacts (feed, report_date);

CREATE INDEX IF NOT EXISTS historical_backfill_item_artifacts_artifact_sha256_idx
    ON historical_backfill_item_artifacts (artifact_sha256);
