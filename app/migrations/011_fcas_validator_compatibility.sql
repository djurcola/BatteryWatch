-- Replace the migration-010 FCAS validators with PostgreSQL-compatible
-- cardinality checks while retaining the exact grouped JSONB contract.

BEGIN;

CREATE OR REPLACE FUNCTION nextday_fcas_service_jsonb_valid(value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $function$
DECLARE
    key_count BIGINT;
    target_type TEXT;
    status_type TEXT;
    actual_type TEXT;
BEGIN
    IF value IS NULL OR jsonb_typeof(value) <> 'object' THEN
        RETURN FALSE;
    END IF;
    SELECT count(*)
    INTO key_count
    FROM jsonb_object_keys(value);
    IF key_count <> 3
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
    key_count BIGINT;
    service_name TEXT;
BEGIN
    IF value IS NULL OR jsonb_typeof(value) <> 'object' THEN
        RETURN FALSE;
    END IF;
    SELECT count(*)
    INTO key_count
    FROM jsonb_object_keys(value);
    IF key_count <> 10
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

COMMIT;
