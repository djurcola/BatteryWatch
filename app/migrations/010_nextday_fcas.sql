-- Retain grouped FCAS evidence projected from the already-registered Next Day
-- UnitSolution bytes.  The map is deliberately one JSONB object per
-- DUID/interval, not one physical row per service.  As with 008, these tables
-- are PostgreSQL/Timescale-ready; operational hypertable conversion remains a
-- separately reviewed deployment concern.

BEGIN;

-- Compare JSONB numbers as JSONB instead of casting arbitrary JSON text, so
-- validation cannot throw on an oversized or non-finite numeric token.  The
-- explicit special-value and type checks keep the fixed service-map contract
-- in the database while rejecting unknown status codes.
CREATE OR REPLACE FUNCTION nextday_fcas_service_jsonb_valid(value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $function$
DECLARE
    target_type TEXT;
    status_type TEXT;
    actual_type TEXT;
BEGIN
    IF value IS NULL OR jsonb_typeof(value) <> 'object' THEN
        RETURN FALSE;
    END IF;
    IF jsonb_object_length(value) <> 3
       OR NOT value ?& ARRAY[
           'target_mw', 'enablement_status', 'actual_availability_mw'
       ] THEN
        RETURN FALSE;
    END IF;

    target_type := jsonb_typeof(value -> 'target_mw');
    status_type := jsonb_typeof(value -> 'enablement_status');
    actual_type := jsonb_typeof(value -> 'actual_availability_mw');
    IF target_type NOT IN ('null', 'number')
       OR status_type NOT IN ('null', 'number')
       OR actual_type NOT IN ('null', 'number') THEN
        RETURN FALSE;
    END IF;

    IF target_type = 'number' AND (
        (value -> 'target_mw') < '0'::jsonb
        OR (value ->> 'target_mw') IN ('NaN', 'Infinity', '-Infinity')
    ) THEN
        RETURN FALSE;
    END IF;
    IF actual_type = 'number' AND (
        (value -> 'actual_availability_mw') < '0'::jsonb
        OR (value ->> 'actual_availability_mw') IN ('NaN', 'Infinity', '-Infinity')
    ) THEN
        RETURN FALSE;
    END IF;
    IF status_type = 'number'
       AND (value ->> 'enablement_status') NOT IN ('0', '1', '2', '3', '4') THEN
        RETURN FALSE;
    END IF;
    RETURN TRUE;
END
$function$;

CREATE OR REPLACE FUNCTION nextday_fcas_map_jsonb_valid(value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $function$
DECLARE
    service_name TEXT;
BEGIN
    IF value IS NULL OR jsonb_typeof(value) <> 'object' THEN
        RETURN FALSE;
    END IF;
    IF jsonb_object_length(value) <> 10
       OR NOT value ?& ARRAY[
           'raise_1s', 'lower_1s', 'raise_6s', 'lower_6s',
           'raise_60s', 'lower_60s', 'raise_5m', 'lower_5m',
           'raise_reg', 'lower_reg'
       ] THEN
        RETURN FALSE;
    END IF;
    FOREACH service_name IN ARRAY ARRAY[
        'raise_1s', 'lower_1s', 'raise_6s', 'lower_6s',
        'raise_60s', 'lower_60s', 'raise_5m', 'lower_5m',
        'raise_reg', 'lower_reg'
    ] LOOP
        IF NOT nextday_fcas_service_jsonb_valid(value -> service_name) THEN
            RETURN FALSE;
        END IF;
    END LOOP;
    RETURN TRUE;
END
$function$;

CREATE TABLE IF NOT EXISTS raw_nextday_fcas_observations (
    artifact_sha256 TEXT NOT NULL
        REFERENCES historical_source_artifacts (artifact_sha256) ON DELETE RESTRICT,
    generator_id TEXT NOT NULL
        REFERENCES generators (generator_id) ON DELETE RESTRICT,
    interval_start TIMESTAMPTZ NOT NULL,
    fcas_services JSONB NOT NULL
        CHECK (nextday_fcas_map_jsonb_valid(fcas_services)),
    intervention SMALLINT NOT NULL CHECK (intervention IN (0, 1)),
    run_number INTEGER NOT NULL CHECK (run_number > 0),
    dispatch_interval TEXT NOT NULL CHECK (dispatch_interval ~ '^[0-9]{11}$'),
    last_changed TIMESTAMPTZ NOT NULL,
    report_timestamp TIMESTAMPTZ NOT NULL,
    downloaded_at TIMESTAMPTZ NOT NULL,
    ingestion_version BIGINT NOT NULL CHECK (ingestion_version >= 0),
    correction_version BIGINT NOT NULL CHECK (correction_version >= 0),
    stored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
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

CREATE INDEX IF NOT EXISTS raw_nextday_fcas_observations_artifact_time_idx
    ON raw_nextday_fcas_observations (artifact_sha256, interval_start ASC);
CREATE INDEX IF NOT EXISTS raw_nextday_fcas_observations_generator_time_idx
    ON raw_nextday_fcas_observations (generator_id, interval_start DESC);

CREATE TABLE IF NOT EXISTS generator_fcas_5m (
    generator_id TEXT NOT NULL
        REFERENCES generators (generator_id) ON DELETE RESTRICT,
    interval_start TIMESTAMPTZ NOT NULL,
    fcas_services JSONB NOT NULL
        CHECK (nextday_fcas_map_jsonb_valid(fcas_services)),
    last_changed TIMESTAMPTZ NOT NULL,
    report_timestamp TIMESTAMPTZ NOT NULL,
    downloaded_at TIMESTAMPTZ NOT NULL,
    intervention SMALLINT NOT NULL CHECK (intervention IN (0, 1)),
    run_number INTEGER NOT NULL CHECK (run_number > 0),
    dispatch_interval TEXT NOT NULL CHECK (dispatch_interval ~ '^[0-9]{11}$'),
    ingestion_version BIGINT NOT NULL CHECK (ingestion_version >= 0),
    correction_version BIGINT NOT NULL CHECK (correction_version >= 0),
    source_artifact_sha256 TEXT NOT NULL
        REFERENCES historical_source_artifacts (artifact_sha256) ON DELETE RESTRICT,
    PRIMARY KEY (generator_id, interval_start),
    UNIQUE (
        generator_id,
        interval_start,
        source_artifact_sha256,
        ingestion_version,
        correction_version
    ),
    CHECK (date_trunc('minute', interval_start) = interval_start),
    CHECK (EXTRACT(MINUTE FROM interval_start)::INTEGER % 5 = 0),
    CHECK (last_changed <= report_timestamp),
    CHECK (report_timestamp <= downloaded_at)
);

CREATE INDEX IF NOT EXISTS generator_fcas_5m_time_idx
    ON generator_fcas_5m (interval_start DESC);
CREATE INDEX IF NOT EXISTS generator_fcas_5m_generator_time_idx
    ON generator_fcas_5m (generator_id, interval_start DESC);

COMMIT;
