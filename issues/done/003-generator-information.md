# 003 — Selected generator information placeholders

## Goal

Add a selected-generator information panel to the deployed BatteryWatch successor, backed by nullable PostgreSQL metadata fields and the existing `/api/generators` response. This is a placeholder-first pass: existing identity/capacity values display normally, while fields awaiting Dan's source display `Not available` rather than invented data.

The legacy `BatteryData.json`/`index.html` is reference evidence only. Do not import its data in this pass.

## User-visible contract

Whenever a generator is selected, show a responsive `Generator information` panel near the top of the dashboard (before chart/loading content). The panel follows the selected DUID and displays these labels in this order:

1. Region
2. Asset Type
3. Site Name
4. Owner
5. Technology Type
6. Fuel Type
7. DUID
8. Status
9. Nameplate Capacity (MW)
10. Storage Capacity (MWh)
11. Commercial Use Date
12. Battery Manufacturer
13. Battery Model
14. Inverter Manufacturer
15. Inverter Model
16. Dispatch Type
17. Status Bucket Summary
18. Fuel Bucket Summary

Known existing values (`region`, `site_name`, `duid`, `capacity_mw`, `storage_capacity_mwh`) must render with concise numeric formatting. Every absent nullable value must render exactly `Not available`. Do not add a second generator selector or a new HTTP request/endpoint.

## Data and API contract

Add additive migration `app/migrations/015_generator_information.sql` and wire it after migration 014 in `app/deploy/migrate.sh`.

Add these nullable `TEXT` columns to `generators`:

- `asset_type`
- `owner_name`
- `technology_type`
- `fuel_type`
- `unit_status`
- `commercial_use_date`
- `battery_manufacturer`
- `battery_model`
- `inverter_manufacturer`
- `inverter_model`
- `dispatch_type`
- `status_bucket_summary`
- `fuel_bucket_summary`

The migration must be idempotent (`ADD COLUMN IF NOT EXISTS`) and must not populate values. Use text for `commercial_use_date` because the known source shape uses month precision such as `Sep-25`; do not invent a day.

Extend `GeneratorMetadata`, PostgreSQL generator SELECT/row mapping/upsert, API `Generator`, fixture metadata, and database conversion. API property `owner` maps to storage field `owner_name`; all other API names match the database names. Optional metadata defaults to `None` so existing call sites remain compatible.

On generator conflict, incoming `NULL` optional metadata must not erase an existing non-null value (`COALESCE` each optional field). Preserve the current revision guard and behavior for existing required fields.

## TDD and acceptance

Use strict RED→GREEN. Add a focused backend test that fails for the missing migration/API/storage contract and run it before production changes. Add a focused frontend contract test that fails before UI/CSS changes and run it before those changes. Record the expected RED failures in your final report.

Acceptance requires:

- `/api/generators` and series generator metadata expose all 13 nullable fields.
- Fixture responses expose those fields as JSON null.
- PostgreSQL reads map populated and null optional fields correctly.
- PostgreSQL upserts bind the optional fields and preserve stored non-null values when incoming fields are null.
- Migration 015 is additive/idempotent, does not backfill data, and is applied after 014.
- The selected-generator panel contains all 18 labels, changes through the existing `selected` state, and displays `Not available` for null/blank values.
- Responsive styling works at the existing 780px breakpoint without horizontal page overflow.
- No legacy `BatteryData.json` values are copied into app config, migration, tests, or runtime data.

## Verification

Run focused tests during RED/GREEN, then:

```bash
PYTHONPATH=app/backend python3 -m unittest discover -s app/backend/tests -p 'test_*.py'
cd app/frontend && npm test -- --run && npm run typecheck && npm run build
cd ../.. && python3 -m compileall -q app/backend && git diff --check && git status --short
```

If local frontend dependencies are absent, report that environment blocker; do not install packages.

## Slice declaration

- Prerequisites: exact clean base `f9e65ed736bc61388c7706de0d78c7ad52c783b4`
- Blocked by: none
- Blocks: production migration and deployment; second-pass metadata import
- Exclusive files/resources: `app/migrations/015_generator_information.sql`, `app/deploy/migrate.sh`, generator storage/API models and tests, `app/frontend/src/main.tsx`, `app/frontend/src/styles.css`, new focused frontend test
- Concurrency group: sequential-only
- Integration owner: Hermes Coder
- Model policy: `implementation-default`
- Targeted verification: commands above
- Expected size: one coherent end-to-end slice; may exceed eight files because schema, repository, API, UI, and their public-contract tests must ship atomically

## Forbidden side effects

Do not commit, push, merge, deploy, access credentials, install packages, modify production, restart services, edit `BatteryData.json`, or process another issue. Leave delivery to Hermes Coder.
