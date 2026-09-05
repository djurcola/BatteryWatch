-- Optional generator information fields. Values are intentionally not backfilled.
BEGIN;

ALTER TABLE generators
    ADD COLUMN IF NOT EXISTS asset_type TEXT,
    ADD COLUMN IF NOT EXISTS owner_name TEXT,
    ADD COLUMN IF NOT EXISTS technology_type TEXT,
    ADD COLUMN IF NOT EXISTS fuel_type TEXT,
    ADD COLUMN IF NOT EXISTS unit_status TEXT,
    ADD COLUMN IF NOT EXISTS commercial_use_date TEXT,
    ADD COLUMN IF NOT EXISTS battery_manufacturer TEXT,
    ADD COLUMN IF NOT EXISTS battery_model TEXT,
    ADD COLUMN IF NOT EXISTS inverter_manufacturer TEXT,
    ADD COLUMN IF NOT EXISTS inverter_model TEXT,
    ADD COLUMN IF NOT EXISTS dispatch_type TEXT,
    ADD COLUMN IF NOT EXISTS status_bucket_summary TEXT,
    ADD COLUMN IF NOT EXISTS fuel_bucket_summary TEXT;

COMMIT;
