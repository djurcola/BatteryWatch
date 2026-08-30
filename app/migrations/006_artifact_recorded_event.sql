-- Allow immutable artifact receipts to append replay-history events.

BEGIN;

ALTER TABLE historical_backfill_events
    DROP CONSTRAINT IF EXISTS historical_backfill_events_event_type_check;

ALTER TABLE historical_backfill_events
    ADD CONSTRAINT historical_backfill_events_event_type_check
    CHECK (
        event_type IN (
            'planned',
            'recovered',
            'claimed',
            'artifact_recorded',
            'completed',
            'failed'
        )
    );

COMMIT;
