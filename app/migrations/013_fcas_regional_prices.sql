BEGIN;

-- Add the nullable, grouped DispatchIS FCAS price projection.  Historical
-- energy-price rows remain valid until their exact retained artifact is replayed.
CREATE OR REPLACE FUNCTION dispatch_fcas_service_price_jsonb_valid(value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    price_value JSONB;
    apc_value JSONB;
    price_text TEXT;
    apc_text TEXT;
BEGIN
    IF jsonb_typeof(value) <> 'object'
       OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(value) AS key)
          <> ARRAY['apc_flag', 'price_aud_per_mw_hour'] THEN
        RETURN FALSE;
    END IF;
    price_value := value -> 'price_aud_per_mw_hour';
    apc_value := value -> 'apc_flag';
    IF price_value IS NULL OR apc_value IS NULL
       OR jsonb_typeof(apc_value) <> 'number' THEN
        RETURN FALSE;
    END IF;
    apc_text := apc_value #>> '{}';
    IF apc_text !~ '^(0|[1-9][0-9]*)$' THEN
        RETURN FALSE;
    END IF;
    PERFORM apc_text::numeric;
    IF jsonb_typeof(price_value) = 'null' THEN
        RETURN TRUE;
    END IF;
    IF jsonb_typeof(price_value) <> 'number' THEN
        RETURN FALSE;
    END IF;
    price_text := price_value #>> '{}';
    IF price_text IN ('NaN', 'Infinity', '-Infinity') OR price_text::numeric < 0 THEN
        RETURN FALSE;
    END IF;
    RETURN TRUE;
EXCEPTION WHEN others THEN
    RETURN FALSE;
END;
$$;

CREATE OR REPLACE FUNCTION dispatch_fcas_price_map_jsonb_valid(value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $$
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
        IF NOT dispatch_fcas_service_price_jsonb_valid(value -> service_name) THEN
            RETURN FALSE;
        END IF;
    END LOOP;
    RETURN TRUE;
EXCEPTION WHEN others THEN
    RETURN FALSE;
END;
$$;

ALTER TABLE nem_price_5m ADD COLUMN IF NOT EXISTS fcas_prices JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'nem_price_5m_fcas_prices_valid'
          AND conrelid = 'nem_price_5m'::regclass
    ) THEN
        ALTER TABLE nem_price_5m ADD CONSTRAINT nem_price_5m_fcas_prices_valid CHECK (
            fcas_prices IS NULL OR dispatch_fcas_price_map_jsonb_valid(fcas_prices)
        );
    END IF;
END;
$$;

COMMIT;
