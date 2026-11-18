# Battery generator onboarding

## Current policy

New battery generators are **not automatically admitted** to BatteryWatch.

The reviewed source of truth is [`../config/battery_assets.json`](../config/battery_assets.json). The production collector loads that file when it starts. There is currently no scheduled process that updates the registry from AEMO's registration or generation-information publications.

This is an intentional fail-closed boundary. Dispatch SCADA contains DUIDs for many kinds of NEM units and does not, by itself, establish that a unit is a battery or provide all required battery metadata.

## What is automatic

Once a reviewed battery DUID is present in `battery_assets.json` and the updated release is deployed:

- the three-minute Dispatch SCADA collector automatically maps matching power observations;
- the collector creates or updates the corresponding row in `generators` without manual SQL;
- the generator appears through `/api/generators` after matching telemetry has been ingested;
- applicable Next Day SOC and FCAS collection accepts that reviewed DUID; and
- future power, SOC, FCAS, and regional-price processing follows the normal collector paths.

## What happens to an unknown DUID

Dispatch SCADA ingestion retains raw observations for every parsed DUID. Only DUIDs present in the reviewed asset registry are promoted into `generator_power_5m` and the user-facing `generators` table.

Consequently, a newly commissioned battery may be visible in retained raw SCADA evidence but will not automatically appear in the application. The Next Day ingestion paths also filter against the reviewed DUID set.

Adding the DUID later does not automatically rematerialize previously ingested immutable artifacts. Historical coverage requires a separately controlled backfill or purpose-built remapping operation, followed by database and API read-back verification.

## Manual onboarding procedure

1. Identify the candidate DUID from an authoritative AEMO registration or generation-information publication.
2. Confirm that the unit is a battery and that its DUID is observable in Dispatch SCADA.
3. Verify all required metadata rather than inferring it:
   - `duid`
   - `site_name`
   - `region`
   - `capacity_mw`
   - `storage_capacity_mwh`
   - `source_id`
   - `source_timestamp`
4. Add one unique record to the `assets` array in `app/config/battery_assets.json`. If the candidate cannot yet meet the admission requirements, add or retain an explicit `excluded` entry with the reason instead of guessing values.
5. Run the asset-loader and relevant collector tests:

   ```sh
   PYTHONPATH=app/backend python3 -m unittest \
     app.backend.tests.test_battery_assets \
     app.backend.tests.test_collector_service \
     app.backend.tests.test_nextday_soc_ingestion
   ```

6. Run the complete backend suite before acceptance:

   ```sh
   PYTHONPATH=app/backend python3 -m unittest discover \
     -s app/backend/tests -p 'test_*.py'
   ```

7. Review and deploy the change through the immutable-release procedure in [`../deploy/README.md`](../deploy/README.md). Do not edit the production database directly to bypass the registry.
8. After deployment, verify collector health and read back:
   - the generator row and reviewed metadata;
   - recent mapped power intervals;
   - `/api/generators` visibility;
   - a bounded `/api/series` query; and
   - SOC/FCAS availability or explicit unavailability, as applicable.

## Recommended future automation

A safe improvement is a scheduled discovery monitor that compares current authoritative AEMO registrations and observed Dispatch SCADA DUIDs with `battery_assets.json`. It should report candidate additions and prepare provenance-bearing records, but keep review-before-admission for battery classification and capacity metadata.

Blind automatic insertion is not recommended: it could admit non-battery units, duplicate or transitional DUIDs, or records with unknown/invalid storage capacity.
