# Historical analytics iteration

## Baseline and release boundary

This iteration starts from the exact accepted application source deployed as
`/home/dan/batterywatch/releases/20260830-cc7912e8` on `192.168.20.42`.
The preserved Git baseline is commit `24c1983e530f624a5734883d9608578d386b36ac`
on branch `agent/batterywatch-historical-analytics` in the `djurcola-agent`
fork. The 58 tracked files below `app/` have aggregate SHA-256
`418ca128c253ff1d3fbfd663fd4c75820cc8f5cdcec30804648c0969a3d84151`.
Owner `main` remains unchanged. The previous immutable release and disabled
`batterywatch.service` remain the rollback target.

## Verified source semantics

The design is based on live public AEMO/NEMWeb artifacts and EMMS Data Model
5.5, not inferred telemetry:

- `Dispatch_SCADA` daily archives contain 288 nested interval ZIPs. Their
  `UNIT_SCADA` rows publish signed `SCADAVALUE` by DUID. Positive means grid
  export/discharge and negative means grid import/charge in BatteryWatch.
- `DispatchIS_Reports` daily archives contain 288 nested interval ZIPs. Their
  `PRICE` rows publish regional RRP, intervention, APC and market-suspension
  status. Their `REGIONSUM` v9 rows publish regional
  `BDU_INITIAL_ENERGY_STORAGE` in MWh. This is regional aggregate data and is
  never an asset SOC.
- `Next_Day_Dispatch` `UNIT_SOLUTION` v6 publishes previous-trading-day,
  five-minute per-DUID `INITIAL_ENERGY_STORAGE` in MWh. It is authoritative
  next-day individual SOC with publication latency, not near-real-time SOC.
  Only non-intervention actual rows are effective; all relevant source control
  fields and raw artifacts are retained.
- Tasmania currently has no published regional BDU SOC value. Missing values
  remain null and are reported, not inferred.
- Maximum storage capacity is a separate, dated AEMO registry observation.
  Percent SOC is calculated only when a reviewed denominator was applicable at
  the interval. The response retains MWh, percent, denominator, denominator
  effective date and provenance.

## Architecture seams

### 1. Archive transport and deterministic planning

`nemweb_archives.py` owns allow-listed archive/current URLs, bounded HTTPS
fetching, daily/monthly outer-archive validation, safe nested ZIP extraction,
and deterministic UTC-range planning. It returns immutable artifact objects;
it does not parse domain rows or write a database.

A request is a half-open UTC range `[start, end)`. The planner maps it to the
fixed NEM market timezone (UTC+10), plans only intersecting local report dates,
and filters inner interval artifacts back to the exact UTC range. The API and
operator CLI reject non-aware timestamps, non-increasing ranges, dates outside
feed availability and ranges above the configured hard cap.

### 2. Append-only evidence and resumable execution

Migration 004 adds:

- `nemweb_archive_artifacts`: immutable downloaded outer artifacts, hashes,
  source URLs, report dates, fetch/publication metadata and raw bytes;
- `ingestion_runs`: requested range, selected feeds, ingestion version,
  lifecycle status, deterministic counters and completion/error summary;
- `ingestion_run_items`: one planned source artifact per run with pending,
  running, completed, replayed or failed status, attempts and counts.

The existing interval artifact/raw tables remain authoritative evidence for
SCADA and DispatchIS. An item is marked complete only after its artifact
transaction commits. On interruption, resuming the same run retries only
non-terminal items. If the data transaction committed before the item status,
existing immutable evidence produces a verified replay and the item becomes
`replayed`. Conflicting bytes for an existing source identity fail closed.
Starting a new run over the same range records a new replay history while the
effective observations remain idempotent.

The backfill is a separate bounded CLI process. It does not stop, reload or
replace `batterywatch-collector.service`; existing uniqueness and guarded
revision ordering make concurrent live/backfill overlap safe. One database
transaction is used per inner artifact, so work is bounded and resumable.
Summaries are JSON with stable key and feed ordering and contain run ID,
requested/effective coverage, planned/completed/replayed/failed counts and the
next resumable item.

### 3. Authoritative SOC products

Migration 005 adds append-only raw `UNIT_SOLUTION` SOC observations, dated
storage-capacity history, richer effective individual SOC fields, and a
separate regional BDU SOC series. It never joins regional SOC to a DUID.

Individual effective SOC fields include `soc_mwh`, nullable `soc_percent`,
nullable `capacity_mwh`, capacity effective timestamp/source, source artifact,
source `LASTCHANGED`, intervention/run status, publication timestamp/status,
ingestion/correction versions and quality flags. The parser keeps valid finite
MWh including zero, rejects ambiguous duplicate effective rows and retains raw
control fields.

Regional BDU SOC is parsed from the same DispatchIS artifact as price and is
persisted in its own regional table. The collector extends its existing
DispatchIS cycle rather than creating a second poller, so each interval ZIP is
fetched once. Missing Tasmania remains an explicit null/unavailable regional
observation.

### 4. Historical query and aggregation

A new bounded database-only historical endpoint preserves the existing API.
Hard maximum range is 366 days and hard maximum output is 2,500 buckets. Preset
resolution is deterministic and UTC anchored:

| Range | Resolution |
| --- | --- |
| up to 24 hours | 5 minutes |
| over 24 hours through 7 days | 15 minutes |
| over 7 days through 30 days | 1 hour |
| over 30 days through 366 days | 6 hours |

An optional resolution may only select an equal or coarser allowed bucket.
Database queries remain bounded by generator/region, range and bucket count.
Long-range responses do not materialize every raw row in Python.

Each bucket returns mean power plus power minimum/maximum, price mean plus price
minimum/maximum, latest individual SOC plus SOC extrema where meaningful,
separate regional SOC, signed net energy, positive exported-energy magnitude,
positive imported-energy magnitude, observed charging cost, observed export
value and observed net energy value. Monetary and energy totals are summed from
raw five-minute observations, never reconstructed from bucket averages.
Coverage reports expected/observed/missing intervals per source, partial
buckets, earliest/latest source times, publication latency/status, source IDs,
resolution and calculation version. Negative prices and extrema are retained.

### 5. Estimate scenarios

Raw observations and observed grid-side calculations are immutable. Financial
values are labelled estimates, never profit or settlement revenue.

The default scenario assumptions are explicit request parameters with bounded
ranges: charging efficiency, discharging efficiency and degradation cost per
MWh. Scenario output is separate from observed grid-side import/export value
and includes the exact formula and assumptions used. The methodology states
that FCAS revenue, contracts, marginal loss factors, network charges,
auxiliary consumption, taxes, fees and all other unavailable settlement inputs
are excluded.

### 6. Dashboard

The dashboard adds 24h, 7d, 30d and validated custom UTC controls. One request
feeds synchronized, zoomable power, regional price and authoritative individual
SOC charts; regional BDU SOC is a separate chart and label. Tooltips preserve
average and extrema and show SOC MWh/percent/capacity provenance and next-day
publication state.

Summary cards show imported/exported energy, observed charging cost, observed
export value, observed estimated net energy value, separate scenario-adjusted
estimate, price/SOC coverage and missing intervals. Loading, error, no-data,
partial/stale and unavailable-SOC states are explicit. Layout remains responsive.

## Vertical-slice DAG

All slices use model-policy profile `implementation-default`, integration owner
Hermes Coder, strict assertion-level RED→GREEN cycles, no commit/push, and
fresh supervisor verification. They run sequentially because they share the
same worktree and public contracts.

### BW-HIST-S1 — archive power/price tracer and resumable run

- prerequisites: exact deployed baseline and source-semantics verification
- blocked-by: none
- blocks: BW-HIST-S2, BW-HIST-S3, BW-HIST-S4
- exclusive files/resources: archive/backfill modules and tests, migration 004,
  migration/runbook wiring, real reduced archive fixtures
- concurrency group: sequential-historical
- integration owner: Hermes Coder
- model-policy profile: implementation-default
- targeted verification: focused archive/backfill/migration tests, complete
  backend suite, Pyright, compileall, shell syntax and diff check
- tracer: one reduced real SCADA daily archive and one reduced real DispatchIS
  daily archive plan, ingest, interrupt, resume and replay end to end
- sizing: deliberate >8-file exception because the public seam includes schema,
  transport, CLI and one end-to-end persistence test; stop/re-slice at the
  175k context notice or material scope drift

### BW-HIST-S2 — individual and regional SOC

- prerequisites: BW-HIST-S1
- blocked-by: BW-HIST-S1
- blocks: BW-HIST-S3, BW-HIST-S4
- exclusive files/resources: SOC parser/storage/backfill integration and tests,
  migration 005, capacity provenance, current DispatchIS collector extension,
  real reduced NextDay/REGIONSUM fixtures
- concurrency group: sequential-historical
- integration owner: Hermes Coder
- model-policy profile: implementation-default
- targeted verification: focused SOC/parser/repository/collector/migration tests,
  complete backend suite, Pyright, compileall and diff check
- tracer: one real reduced NextDay DUID row and one DispatchIS regional set
  through parser, persistence, replay and read-back with null-Tasmania proof
- sizing: deliberate >8-file exception for one authoritative end-to-end seam;
  re-slice parser/storage from collector integration if context or diff drifts

### BW-HIST-S3 — bounded historical API and scenario aggregation

- prerequisites: BW-HIST-S1 and BW-HIST-S2
- blocked-by: BW-HIST-S1, BW-HIST-S2
- blocks: BW-HIST-S4
- exclusive files/resources: API models, historical query repository/service,
  migration 006 aggregate/index structures and focused tests
- concurrency group: sequential-historical
- integration owner: Hermes Coder
- model-policy profile: implementation-default
- targeted verification: focused raw/downsample/bounds/extrema/coverage/scenario
  tests, PostgreSQL migration/query tests, complete backend suite, Pyright,
  compileall and diff check
- tracer: 30-day database query returning bounded one-hour buckets while exact
  raw totals, negative-price extrema and provenance remain correct
- sizing: expected within eight production/test paths; split query and API model
  only if the 175k context notice coincides with incomplete behavior

### BW-HIST-S4 — historical dashboard

- prerequisites: BW-HIST-S3
- blocked-by: BW-HIST-S3
- blocks: deployment and browser acceptance
- exclusive files/resources: frontend API/types/components/styles/tests and
  user-facing methodology documentation
- concurrency group: sequential-historical
- integration owner: Hermes Coder
- model-policy profile: implementation-default
- targeted verification: frontend tests/typecheck/build, static DOM verification,
  backend suite, Pyright, npm audit and diff check
- tracer: 30-day control renders synchronized real-contract charts, separate
  regional SOC, summaries and explicit partial/unavailable states
- sizing: expected within eight frontend paths; split components from final
  visual polish if runtime context or visual scope drifts

## Deployment and acceptance ladder

1. Freeze and independently review the exact local source hash.
2. Commit/push only with the isolated `djurcola-agent` identity and update an
   unmerged PR against `djurcola/BatteryWatch:main`.
3. Record current release/service/database/listener evidence and create a fresh
   custom-format PostgreSQL backup. Verify SHA-256, `pg_restore --list`, isolated
   restore, database integrity, indexes and domain-count parity.
4. Stage an immutable candidate release without changing `current`; build its
   venv/frontend and apply migrations under the backup gate.
5. Run fixture and reduced-real migration/backfill acceptance in an isolated
   database, then run candidate API on port 18080 against the real database.
6. Switch `current`, restart only the two BatteryWatch user units, verify
   database mode, then execute the bounded 30-day backfill while the live
   collector remains enabled and active.
7. Prove data coverage/counts, idempotent replay, deterministic resume summary,
   API bounds/downsampling/estimates, browser controls/charts, loopback-only
   PostgreSQL and at least two subsequent live collection intervals.
8. Verify rollback by exercising the documented prior-release service path
   without deleting additive data; restore the candidate after the rollback
   check only if all acceptance gates remain green.

Cloudflare, the user-managed reverse proxy, unrelated hosts, credentials and
owner main are outside this iteration.
