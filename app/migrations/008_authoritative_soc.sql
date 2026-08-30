-- Add authoritative individual Next Day SOC observations and provenance.

BEGIN;

-- Widen the historical backfill/artifact feed contracts without rewriting
-- existing SCADA or price rows.  The monthly Next Day archive is keyed by the
-- first day of its content month.
ALTER TABLE historical_backfill_items
    DROP CONSTRAINT IF EXISTS historical_backfill_items_feed_check,
    ADD CONSTRAINT historical_backfill_items_feed_check
        CHECK (feed IN ('dispatch_price', 'dispatch_scada', 'nextday_soc'));

ALTER TABLE historical_backfill_events
    DROP CONSTRAINT IF EXISTS historical_backfill_events_feed_check,
    ADD CONSTRAINT historical_backfill_events_feed_check
        CHECK (feed IN ('dispatch_price', 'dispatch_scada', 'nextday_soc'));

ALTER TABLE historical_source_artifacts
    DROP CONSTRAINT IF EXISTS historical_source_artifacts_feed_check,
    ADD CONSTRAINT historical_source_artifacts_feed_check
        CHECK (feed IN ('dispatch_price', 'dispatch_scada', 'nextday_soc')),
    DROP CONSTRAINT IF EXISTS historical_source_artifacts_archive_identity_ck,
    ADD CONSTRAINT historical_source_artifacts_archive_identity_ck CHECK (
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
        OR (
            feed = 'nextday_soc'
            AND report_date = date_trunc('month', report_date)::date
            AND source_url =
                'https://www.nemweb.com.au/REPORTS/ARCHIVE/Next_Day_Dispatch/' || filename
            AND filename =
                'PUBLIC_NEXT_DAY_DISPATCH_' || to_char(report_date, 'YYYYMM') || '01.zip'
        )
    );

ALTER TABLE historical_backfill_item_artifacts
    DROP CONSTRAINT IF EXISTS historical_backfill_item_artifacts_feed_check,
    ADD CONSTRAINT historical_backfill_item_artifacts_feed_check
        CHECK (feed IN ('dispatch_price', 'dispatch_scada', 'nextday_soc'));

CREATE TABLE IF NOT EXISTS raw_nextday_soc_observations (
    artifact_sha256 TEXT NOT NULL
        REFERENCES historical_source_artifacts (artifact_sha256) ON DELETE RESTRICT,
    generator_id TEXT NOT NULL
        REFERENCES generators (generator_id) ON DELETE RESTRICT,
    interval_start TIMESTAMPTZ NOT NULL,
    soc_mwh DOUBLE PRECISION CHECK (
        soc_mwh IS NULL
        OR (
            soc_mwh >= 0
            AND soc_mwh::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')
        )
    ),
    intervention SMALLINT NOT NULL CHECK (intervention IN (0, 1)),
    run_number INTEGER NOT NULL CHECK (run_number > 0),
    dispatch_interval TEXT NOT NULL CHECK (dispatch_interval ~ '^[0-9]{11}$'),
    last_changed TIMESTAMPTZ NOT NULL,
    report_timestamp TIMESTAMPTZ NOT NULL,
    downloaded_at TIMESTAMPTZ NOT NULL,
    ingestion_version BIGINT NOT NULL CHECK (ingestion_version >= 0),
    correction_version BIGINT NOT NULL CHECK (correction_version >= 0),
    PRIMARY KEY (
        artifact_sha256,
        generator_id,
        interval_start,
        intervention,
        run_number,
        ingestion_version,
        correction_version
    ),
    CHECK (date_trunc('minute', interval_start) = interval_start),
    CHECK (EXTRACT(MINUTE FROM interval_start)::INTEGER % 5 = 0),
    CHECK (last_changed <= report_timestamp),
    CHECK (report_timestamp <= downloaded_at)
);

CREATE INDEX IF NOT EXISTS raw_nextday_soc_observations_artifact_time_idx
    ON raw_nextday_soc_observations (artifact_sha256, interval_start ASC);
CREATE INDEX IF NOT EXISTS raw_nextday_soc_observations_generator_time_idx
    ON raw_nextday_soc_observations (generator_id, interval_start DESC);

ALTER TABLE generator_soc_5m
    ADD COLUMN IF NOT EXISTS soc_mwh DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS capacity_mwh DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS capacity_effective_from TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS capacity_effective_to TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS capacity_source_id TEXT,
    ADD COLUMN IF NOT EXISTS capacity_source_timestamp TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS report_timestamp TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS downloaded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS intervention SMALLINT,
    ADD COLUMN IF NOT EXISTS run_number INTEGER,
    ADD COLUMN IF NOT EXISTS dispatch_interval TEXT,
    ADD COLUMN IF NOT EXISTS source_artifact_sha256 TEXT
        REFERENCES historical_source_artifacts (artifact_sha256) ON DELETE RESTRICT;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'generator_soc_5m_authoritative_soc_check'
          AND conrelid = 'generator_soc_5m'::regclass
    ) THEN
        ALTER TABLE generator_soc_5m
            ADD CONSTRAINT generator_soc_5m_authoritative_soc_check
            CHECK (
                soc_mwh IS NULL
                OR (
                    soc_mwh >= 0
                    AND soc_mwh::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')
                )
            );
    END IF;
END
$migration$;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'generator_soc_5m_capacity_provenance_check'
          AND conrelid = 'generator_soc_5m'::regclass
    ) THEN
        ALTER TABLE generator_soc_5m
            ADD CONSTRAINT generator_soc_5m_capacity_provenance_check
            CHECK (
                source_artifact_sha256 IS NULL
                OR (
                    (
                        soc_percent IS NULL
                        AND num_nonnulls(
                            capacity_mwh,
                            capacity_effective_from,
                            capacity_effective_to,
                            capacity_source_id,
                            capacity_source_timestamp
                        ) = 0
                    )
                    OR (
                        soc_percent IS NOT NULL
                        AND soc_mwh IS NOT NULL
                        AND num_nonnulls(
                            capacity_mwh,
                            capacity_effective_from,
                            capacity_source_id,
                            capacity_source_timestamp
                        ) = 4
                        AND capacity_mwh > 0
                        AND capacity_mwh::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')
                        AND soc_mwh <= capacity_mwh
                        AND abs(soc_percent - (100.0 * soc_mwh / capacity_mwh)) <= 0.000001
                        AND capacity_effective_from <= interval_start
                        AND (
                            capacity_effective_to IS NULL
                            OR (
                                capacity_effective_from < capacity_effective_to
                                AND interval_start < capacity_effective_to
                            )
                        )
                        AND char_length(capacity_source_id) BETWEEN 1 AND 255
                        AND capacity_source_id = btrim(capacity_source_id)
                        AND capacity_source_timestamp <= interval_start
                    )
                )
            );
    END IF;
END
$migration$;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'generator_soc_5m_authoritative_metadata_check'
          AND conrelid = 'generator_soc_5m'::regclass
    ) THEN
        ALTER TABLE generator_soc_5m
            ADD CONSTRAINT generator_soc_5m_authoritative_metadata_check
            CHECK (
                (
                    num_nonnulls(
                        source_artifact_sha256,
                        report_timestamp,
                        downloaded_at,
                        intervention,
                        run_number,
                        dispatch_interval,
                        soc_mwh,
                        capacity_mwh,
                        capacity_effective_from,
                        capacity_effective_to,
                        capacity_source_id,
                        capacity_source_timestamp
                    ) = 0
                )
                OR (
                    num_nonnulls(
                        source_artifact_sha256,
                        report_timestamp,
                        downloaded_at,
                        intervention,
                        run_number,
                        dispatch_interval
                    ) = 6
                    AND
                    source_artifact_sha256 ~ '^[0-9a-f]{64}$'
                    AND report_timestamp <= downloaded_at
                    AND intervention IN (0, 1)
                    AND run_number > 0
                    AND dispatch_interval ~ '^[0-9]{11}$'
                )
            );
    END IF;
END
$migration$;

COMMIT;
