# BatteryWatch successor

This is the standalone successor to the original BatteryWatch static site. The first vertical slice proves a new local API-to-chart path using deterministic five-minute fixtures.

## Local development

Backend:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r app/backend/requirements.txt
uvicorn batterywatch_api.main:app --app-dir app/backend --host 0.0.0.0 --port 8080
```

Frontend, in a second shell:

```bash
npm --prefix app/frontend install
npm --prefix app/frontend run dev
```

Open `http://127.0.0.1:5173`. The Vite development server proxies `/api` to the local API on `http://127.0.0.1:8080`.

After a production frontend build, the API serves `app/frontend/dist` when present. The deployment contract is plain HTTP on `0.0.0.0:8080`, so the user-managed reverse proxy can terminate HTTPS and forward to `http://192.168.20.42:8080`. Database and ingestion/admin endpoints are not exposed by this slice.

## Data semantics

- Positive battery power means discharge/export; negative means charge/import.
- Every point is a five-minute interval and uses `energy_mwh = power_mw × 5/60`.
- Price is represented as AEMO regional RRP-shaped data in AUD/MWh.
- Missing prices remain unavailable (`null`) and are reflected in coverage.
- Gross discharge value, charging cost, and net energy value are estimates, not actual profit. They exclude efficiency losses, auxiliary consumption, degradation, FCAS, network charges, contracts, taxes, and fees.

The fixture includes discharge, charging, zero output, a negative price, missing price, and nullable SOC cases. TimescaleDB/PostgreSQL persistence, real AEMO ingestion, historical backfill, and four-second telemetry are later slices; four-second telemetry is intentionally outside v1.

## S2a storage contract

The S2a persistence seam is defined in `backend/batterywatch_api/storage.py` and
`migrations/001_initial_schema.sql`. Records use UTC-aware timestamps, require
five-minute interval alignment, retain source identity and timestamps, and
carry ingestion and correction versions. Power uses positive discharge/export
and negative charge/import. Zero is a real measurement; missing price and SOC
remain `null`/unavailable and are never inferred from power.

The logical key is generator plus interval for power/SOC and region plus
interval for NEM price. The deterministic repository keeps one effective
record per logical key: an exact replay is a no-op, a newer correction replaces
the effective record, and a stale record cannot regress it. The SQL migration
provides PostgreSQL/Timescale-ready tables and uniqueness/check constraints but
does not create a database, role, credential, extension, or service.

The S2b code slice adds a production-replaceable `PostgreSQLRepository` using a
caller-supplied DB-API connection and parameterized SQL. It preserves this
storage boundary, keeps only the winning effective record (revision history is
not retained), and does not connect to or activate a database. The existing
fixture repository and API remain the local/deployed baseline.

Real database provisioning and activation are a separate supervisor-only gate:
against a private PostgreSQL/TimescaleDB instance, the supervisor must apply
the migration and verify write/read-back, duplicate replay, correction,
backup, and isolated restore before any live AEMO data is written. This slice
does not perform that provisioning or validation.

## S2c AEMO dispatch-price parser

`backend/batterywatch_api/aemo.py` parses canonical dispatch-price CSV rows into
`RegionalPrice5m` records. It requires `SETTLEMENTDATE`, `REGIONID`, and `RRP`,
rejects malformed or duplicate logical intervals, requires an explicit timezone
for offset-free source timestamps, normalizes records to UTC/five-minute
boundaries, and preserves blank, zero, and negative RRP semantics. `RUNNO`,
`INTERVENTION`, and `APCFLAG` are retained as quality metadata; ingestion and
correction revisions are supplied by the coordinator rather than guessed from
the source row. Live AEMO fetching, scheduling, persistence, and backfill remain
separate gates.
