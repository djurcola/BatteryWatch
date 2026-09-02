-- BatteryWatch v1 PostgreSQL/Timescale-ready schema.
-- This migration intentionally does not create the database, role, credentials,
-- or Timescale extension. Provisioning and optional hypertable conversion belong
-- to the separately verified S2b deployment step.
--
-- Each table stores one effective record per logical key. The guarded upserts
-- replace that effective record only when the revision tuple advances;
-- revision history is not retained by this migration and requires a separate,
-- explicitly designed audit table if it is needed later.

CREATE TABLE IF NOT EXISTS generators (
    generator_id TEXT PRIMARY KEY,
    site_name TEXT NOT NULL,
    region TEXT NOT NULL,
    capacity_mw DOUBLE PRECISION NOT NULL CHECK (
        capacity_mw > 0
        AND capacity_mw::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')
    ),
    storage_capacity_mwh DOUBLE PRECISION NOT NULL CHECK (
        storage_capacity_mwh > 0
        AND storage_capacity_mwh::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')
    ),
    source_id TEXT NOT NULL,
    source_timestamp TIMESTAMPTZ NOT NULL,
    ingestion_version BIGINT NOT NULL DEFAULT 0 CHECK (ingestion_version >= 0),
    correction_version BIGINT NOT NULL DEFAULT 0 CHECK (correction_version >= 0),
    data_start TIMESTAMPTZ,
    data_end TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (data_end IS NULL OR data_start IS NULL OR data_end > data_start)
);

CREATE TABLE IF NOT EXISTS generator_power_5m (
    generator_id TEXT NOT NULL REFERENCES generators (generator_id),
    interval_start TIMESTAMPTZ NOT NULL,
    power_mw DOUBLE PRECISION NOT NULL,
    source_id TEXT NOT NULL,
    source_timestamp TIMESTAMPTZ NOT NULL,
    ingestion_version BIGINT NOT NULL CHECK (ingestion_version >= 0),
    correction_version BIGINT NOT NULL DEFAULT 0 CHECK (correction_version >= 0),
    quality_flags TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (generator_id, interval_start),
    UNIQUE (generator_id, interval_start, source_id, source_timestamp, ingestion_version, correction_version),
    CHECK (date_trunc('minute', interval_start) = interval_start),
    CHECK (EXTRACT(MINUTE FROM interval_start)::INTEGER % 5 = 0),
    CHECK (power_mw::TEXT NOT IN ('NaN', 'Infinity', '-Infinity'))
);

CREATE TABLE IF NOT EXISTS generator_soc_5m (
    generator_id TEXT NOT NULL REFERENCES generators (generator_id),
    interval_start TIMESTAMPTZ NOT NULL,
    soc_percent DOUBLE PRECISION CHECK (
        soc_percent IS NULL
        OR (
            soc_percent BETWEEN 0 AND 100
            AND soc_percent::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')
        )
    ),
    source_id TEXT NOT NULL,
    source_timestamp TIMESTAMPTZ NOT NULL,
    ingestion_version BIGINT NOT NULL CHECK (ingestion_version >= 0),
    correction_version BIGINT NOT NULL DEFAULT 0 CHECK (correction_version >= 0),
    quality_flags TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (generator_id, interval_start),
    UNIQUE (generator_id, interval_start, source_id, source_timestamp, ingestion_version, correction_version),
    CHECK (date_trunc('minute', interval_start) = interval_start),
    CHECK (EXTRACT(MINUTE FROM interval_start)::INTEGER % 5 = 0)
);

CREATE TABLE IF NOT EXISTS nem_price_5m (
    region TEXT NOT NULL,
    interval_start TIMESTAMPTZ NOT NULL,
    price_aud_per_mwh NUMERIC(15, 5),
    price_status TEXT NOT NULL CHECK (price_status IN ('available', 'negative', 'missing')),
    intervention INTEGER NOT NULL DEFAULT 0 CHECK (intervention >= 0),
    apc_flag INTEGER NOT NULL DEFAULT 0 CHECK (apc_flag >= 0),
    market_suspended BOOLEAN NOT NULL DEFAULT FALSE,
    source_id TEXT NOT NULL,
    source_timestamp TIMESTAMPTZ NOT NULL,
    ingestion_version BIGINT NOT NULL CHECK (ingestion_version >= 0),
    correction_version BIGINT NOT NULL DEFAULT 0 CHECK (correction_version >= 0),
    quality_flags TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (region, interval_start),
    UNIQUE (region, interval_start, source_id, source_timestamp, ingestion_version, correction_version),
    CHECK (date_trunc('minute', interval_start) = interval_start),
    CHECK (EXTRACT(MINUTE FROM interval_start)::INTEGER % 5 = 0),
    CHECK (
        price_aud_per_mwh IS NULL
        OR price_aud_per_mwh::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')
    ),
    CHECK (
        (price_aud_per_mwh IS NULL AND price_status = 'missing')
        OR (price_aud_per_mwh IS NOT NULL AND price_status = 'available' AND price_aud_per_mwh >= 0)
        OR (price_aud_per_mwh IS NOT NULL AND price_status = 'negative' AND price_aud_per_mwh < 0)
    )
);

CREATE INDEX IF NOT EXISTS generator_power_5m_time_idx
    ON generator_power_5m (interval_start DESC);
CREATE INDEX IF NOT EXISTS generator_soc_5m_time_idx
    ON generator_soc_5m (interval_start DESC);
CREATE INDEX IF NOT EXISTS nem_price_5m_time_idx
    ON nem_price_5m (interval_start DESC);

-- On a host with TimescaleDB, a separately reviewed operational migration may
-- convert the three *_5m tables into hypertables after this schema is applied.
