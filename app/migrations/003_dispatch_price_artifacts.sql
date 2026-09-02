-- Immutable official DispatchIS artifacts retained before regional price upserts.

CREATE TABLE IF NOT EXISTS dispatch_price_artifacts (
    source_artifact_id TEXT PRIMARY KEY CHECK (
        source_artifact_id <> '' AND source_artifact_id ~ '^[0-9]+$'
    ),
    source_url TEXT NOT NULL CHECK (source_url <> '' AND source_url ~ '^https://'),
    zip_filename TEXT NOT NULL CHECK (btrim(zip_filename) <> ''),
    csv_member_name TEXT NOT NULL CHECK (btrim(csv_member_name) <> ''),
    report_timestamp TIMESTAMPTZ NOT NULL,
    zip_sha256 TEXT NOT NULL CHECK (zip_sha256 ~ '^[0-9a-f]{64}$'),
    raw_zip BYTEA NOT NULL CHECK (octet_length(raw_zip) > 0),
    stored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        source_url =
            'https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/' || zip_filename
    ),
    CHECK (
        zip_filename ~
            ('^PUBLIC_DISPATCHIS_[0-9]{12}_' || source_artifact_id || '[.]zip$')
    ),
    CHECK (csv_member_name = left(zip_filename, -4) || '.CSV')
);

CREATE INDEX IF NOT EXISTS dispatch_price_artifacts_report_timestamp_idx
    ON dispatch_price_artifacts (report_timestamp DESC);
