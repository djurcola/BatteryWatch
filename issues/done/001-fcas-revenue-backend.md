# Slice: FR-1 Backend gross FCAS payment estimate

## Outcome

`GET /api/fcas` returns a transparent, bounded estimated gross FCAS enablement payment by joining finalized per-DUID targets to same-interval unrounded regional FCAS prices parsed from BatteryWatch's existing retained DispatchIS artifacts. Missing target or price data remains null and coverage is explicit.

## Requirements covered

REQ-FR-1, REQ-FR-2, REQ-FR-3, REQ-FR-4.

Authoritative design: `.hermes/specs/2026-09-04-fcas-revenue-estimate-design.md`. Read it completely before editing.

## Non-goals

- UI work (FR-2 owns it).
- Actual participant settlement, net profit, FPP, FCAS cost recovery, bids/offers, contracts, taxes, fees, degradation, or verified physical response.
- New external feeds/downloaders.
- Production database work, backfill/replay, deployment, package installation, or service changes.
- Changing energy valuation.

## Prerequisites

None. Baseline is `1c8fd7280af81696349e992bf24e180266be337f`; accepted pre-existing files are this issue and the design spec.

## Blocked by

None.

## Blocks

FR-2.

## Exclusive files/resources

- `app/backend/batterywatch_api/aemo.py`
- `app/backend/batterywatch_api/storage.py`
- `app/backend/batterywatch_api/dispatch_price_ingestion.py`
- `app/backend/batterywatch_api/main.py`
- `app/backend/tests/test_aemo.py`
- `app/backend/tests/test_storage.py`
- `app/backend/tests/test_dispatch_price_ingestion.py`
- `app/backend/tests/test_api_fcas.py`
- one new numbered migration and its focused migration-contract test if needed

Do not edit frontend files.

## Concurrency group

Sequential.

## Integration owner

Hermes Coder.

## Model-policy profile

implementation-default.

## Expected change surface

Approximately 9–10 files and 250–400 changed lines. This deliberately exceeds the eight-file warning because parser, persistence, immutable replay, API seam, migration, and behavior tests form one atomic tracer through the real data path. Keep code changes narrowly additive.

## Implementation notes and public seam

1. Use strict TDD. For each behavior, add one failing test, run it and confirm the expected failure, then implement minimally and rerun.
2. Inspect the live `DISPATCH_PRICE_FIELDS_V5` ordering in `aemo.py`; map all ten fields exactly:
   - `raise_1s` → `RAISE1SECRRP`, APC `RAISE1SECAPCFLAG`
   - `lower_1s` → `LOWER1SECRRP`, APC `LOWER1SECAPCFLAG`
   - `raise_6s` → `RAISE6SECRRP`, APC `RAISE6SECAPCFLAG`
   - `lower_6s` → `LOWER6SECRRP`, APC `LOWER6SECAPCFLAG`
   - `raise_60s` → `RAISE60SECRRP`, APC `RAISE60SECAPCFLAG`
   - `lower_60s` → `LOWER60SECRRP`, APC `LOWER60SECAPCFLAG`
   - `raise_5m` → `RAISE5MINRRP`, APC `RAISE5MINAPCFLAG`
   - `lower_5m` → `LOWER5MINRRP`, APC `LOWER5MINAPCFLAG`
   - `raise_reg` → `RAISEREGRRP`, APC `RAISEREGAPCFLAG`
   - `lower_reg` → `LOWERREGRRP`, APC `LOWERREGAPCFLAG`
3. Extend the existing regional-price domain record with one fixed-key grouped service map rather than creating one DB row per service. Preserve current energy RRP/status behavior. Use clear types and canonical serialization.
4. Add migration `012_*` (011 exists) to add the grouped JSONB projection to `nem_price_5m`, nullable for historical compatibility, with a fail-closed immutable validation function/check: exact ten service keys, each object containing nullable finite non-negative `price_aud_per_mw_hour` and a non-negative integer `apc_flag`; reject booleans, arrays, unknown keys, NaN, Infinity, malformed numbers. Follow the database's existing validator patterns.
5. The parser must fail closed for missing schema, field-count mismatch, invalid/non-finite/negative service prices, invalid APC flags, duplicates/conflicts, and unexpected versions. A missing wire value maps to null rather than zero.
6. Update dispatch-price ingestion INSERT/UPSERT and reads. Exact immutable artifact replay may populate a previously-null FCAS projection only when the stored energy row and provenance/effective precedence are identical; never overwrite a populated map or weaken conflicting-byte/correction rules. Read all surrounding ingestion logic before editing.
7. Add bounded storage read support needed by `/api/fcas`; avoid N+1 queries. Join only one requested generator's configured region, requested half-open interval, and selected services.
8. Per interval/service: `estimated_payment_aud = target_mw * price_aud_per_mw_hour / 12`. Return null unless both operands are non-null; preserve true zero. Do not use actual availability in the payment formula.
9. Extend existing FCAS service points with nullable `price_aud_per_mw_hour` and `estimated_payment_aud`. Extend each service summary with nullable/number payment total and target/price/payment coverage counts. Add one response-level estimate object with exact label, formula, units, exclusions, total and coverage. Choose precise names and keep response backward compatible.
10. Generic repository/storage failures retain the existing sanitized 503 behavior. Malformed stored maps fail closed.
11. The returned estimate language must say **estimated gross FCAS enablement payment** and preserve the finalized-target/not-verified-response and publication-delay semantics. Explicitly exclude settlement adjustments, FPP, cost recovery, contracts, fees, taxes, and verified physical response.
12. No public web research is required: the reviewed primary-source decisions and citations are recorded in the design. Do not broaden scope.

## TDD/test plan

- First failing test: parser exposes the ten exact FCAS RRP/APC mappings while preserving energy price parsing.
- Add one failing behavior at a time for malformed/null/zero inputs, typed map validation, migration contract, persistence, safe additive replay, exact formula, partial coverage, service filtering, and generic 503.
- Focused checks after each cycle:
  - `PYTHONPATH=app/backend python3 -m unittest app.backend.tests.test_aemo`
  - `PYTHONPATH=app/backend python3 -m unittest app.backend.tests.test_dispatch_price_ingestion`
  - `PYTHONPATH=app/backend python3 -m unittest app.backend.tests.test_api_fcas`
  - relevant storage/migration tests discovered in this repo
- Broader check: complete backend suite.

Record RED and GREEN commands/results in your final report.

## Verification command

From repository root:

`PYTHONPATH=app/backend python3 -m unittest app.backend.tests.test_aemo app.backend.tests.test_dispatch_price_ingestion app.backend.tests.test_api_fcas && PYTHONPATH=app/backend python3 -m unittest discover -s app/backend/tests -p 'test_*.py' && python3 -m py_compile app/backend/batterywatch_api/*.py && git diff --check`

If the project has a configured Pyright command/environment, run it too; do not install dependencies.

## Forbidden side effects

Do not commit, push, merge, publish, deploy, install packages, access credentials, alter a live/local database, run migrations, replay artifacts, restart services, edit generated `app/frontend/dist`, or handle FR-2. Do not move this issue manually; the orchestrator owns issue movement.

## Acceptance evidence

- All ten authoritative service mappings proven.
- Exact `MW × AUD/MW/hour ÷ 12` formula proven, including true zero and null cases.
- Additive migration and replay rules proven without weakening immutable conflict/correction semantics.
- API bounds/filtering/coverage and sanitized errors proven.
- Complete backend suite, compilation, optional available Pyright, and diff check reported with actual counts.
- Changed files and residual risks listed.

## Status

complete
