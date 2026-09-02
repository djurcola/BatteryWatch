-- Add runtime columns already required by the historical backfill ledger code.

BEGIN;

ALTER TABLE historical_backfill_items
    ADD COLUMN IF NOT EXISTS last_error TEXT;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'historical_backfill_items_last_error_check'
          AND conrelid = 'historical_backfill_items'::regclass
    ) THEN
        ALTER TABLE historical_backfill_items
            ADD CONSTRAINT historical_backfill_items_last_error_check
            CHECK (
                last_error IS NULL
                OR (
                    char_length(last_error) BETWEEN 1 AND 2048
                    AND last_error = btrim(last_error)
                    AND position(E'\r' IN last_error) = 0
                    AND position(E'\n' IN last_error) = 0
                )
            );
    END IF;
END
$migration$;

ALTER TABLE historical_backfill_events
    ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'historical_backfill_events_details_check'
          AND conrelid = 'historical_backfill_events'::regclass
    ) THEN
        ALTER TABLE historical_backfill_events
            ADD CONSTRAINT historical_backfill_events_details_check
            CHECK (jsonb_typeof(details) = 'object');
    END IF;
END
$migration$;

COMMIT;
