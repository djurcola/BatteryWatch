-- Raw verified Dispatch SCADA evidence is retained before later identity mapping.

CREATE TABLE IF NOT EXISTS dispatch_scada_artifacts (
    source_artifact_id TEXT PRIMARY KEY CHECK (source_artifact_id <> '' AND source_artifact_id ~ '^[0-9]+$'),
    source_url TEXT NOT NULL CHECK (source_url <> '' AND source_url ~ '^https://'),
    zip_filename TEXT NOT NULL CHECK (btrim(zip_filename) <> ''),
    csv_member_name TEXT NOT NULL CHECK (btrim(csv_member_name) <> ''),
    report_timestamp TIMESTAMPTZ NOT NULL,
    zip_sha256 TEXT NOT NULL CHECK (zip_sha256 ~ '^[0-9a-f]{64}$'),
    raw_zip BYTEA NOT NULL CHECK (octet_length(raw_zip) > 0),
    stored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (source_url = 'https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/' || zip_filename),
    CHECK (zip_filename ~ ('^PUBLIC_DISPATCHSCADA_[0-9]{12}_' || source_artifact_id || '[.]zip$')),
    CHECK (csv_member_name = left(zip_filename, -4) || '.CSV')
);

CREATE TABLE IF NOT EXISTS raw_dispatch_scada_observations (
    source_artifact_id TEXT NOT NULL REFERENCES dispatch_scada_artifacts (source_artifact_id) ON DELETE RESTRICT,
    duid TEXT NOT NULL CHECK (btrim(duid) <> ''),
    interval_start TIMESTAMPTZ NOT NULL,
    power_mw DOUBLE PRECISION NOT NULL CHECK (power_mw::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')),
    source_timestamp TIMESTAMPTZ NOT NULL,
    ingestion_version BIGINT NOT NULL DEFAULT 0 CHECK (ingestion_version >= 0),
    correction_version BIGINT NOT NULL DEFAULT 0 CHECK (correction_version >= 0),
    stored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_artifact_id, duid, interval_start),
    CHECK (date_trunc('minute', interval_start) = interval_start),
    CHECK (EXTRACT(MINUTE FROM interval_start)::INTEGER % 5 = 0)
);

CREATE INDEX IF NOT EXISTS dispatch_scada_artifacts_report_timestamp_idx ON dispatch_scada_artifacts (report_timestamp DESC);
CREATE INDEX IF NOT EXISTS raw_dispatch_scada_observations_artifact_interval_idx ON raw_dispatch_scada_observations (source_artifact_id, interval_start DESC);
CREATE INDEX IF NOT EXISTS raw_dispatch_scada_observations_interval_idx ON raw_dispatch_scada_observations (interval_start DESC);
