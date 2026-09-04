-- Schema-qualify nested FCAS validator calls so pg_restore can validate
-- table data while it holds an empty search_path.

BEGIN;

CREATE OR REPLACE FUNCTION public.nextday_fcas_map_jsonb_valid(value JSONB)
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
        IF NOT public.nextday_fcas_service_jsonb_valid(value -> service_name) THEN
            RETURN FALSE;
        END IF;
    END LOOP;
    RETURN TRUE;
END
$function$;

CREATE OR REPLACE FUNCTION public.dispatch_fcas_price_map_jsonb_valid(value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $function$
DECLARE
    service_name TEXT;
    expected_keys TEXT[] := ARRAY[
        'lower_1s', 'lower_5m', 'lower_60s', 'lower_6s', 'lower_reg',
        'raise_1s', 'raise_5m', 'raise_60s', 'raise_6s', 'raise_reg'
    ];
BEGIN
    IF jsonb_typeof(value) <> 'object'
       OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(value) AS key)
          <> expected_keys THEN
        RETURN FALSE;
    END IF;
    FOREACH service_name IN ARRAY expected_keys LOOP
        IF NOT public.dispatch_fcas_service_price_jsonb_valid(value -> service_name) THEN
            RETURN FALSE;
        END IF;
    END LOOP;
    RETURN TRUE;
EXCEPTION WHEN others THEN
    RETURN FALSE;
END;
$function$;

COMMIT;
