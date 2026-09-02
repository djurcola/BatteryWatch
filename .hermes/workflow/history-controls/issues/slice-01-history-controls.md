# Slice: HC-01 Multi-day history controls

## Outcome

A user can load and navigate 24-hour, 7-day, and 30-day windows through a gap-aware, bounded API response.

## Requirements covered: REQ-1 through REQ-8

## Non-goals

Database/backfill/collector/service/deployment work; custom unbounded ranges; aggregate tables; imputation.

## Prerequisites: none
## Blocked by: none
## Blocks: operator-gated historical backfill and live deployment

## Exclusive files/resources

`app/backend/batterywatch_api/main.py`, `app/backend/batterywatch_api/models.py`, focused API tests, `app/frontend/src`, frontend tests/styles. One worker owns the worktree.

## Concurrency group: sequential
## Integration owner: Hermes Coder
## Model-policy profile: implementation-default

## Expected change surface

Approximately 7-9 files and 250-400 changed lines. The wider count is justified by one public behavior crossing FastAPI response models, API tests, deterministic frontend range state, React controls, styles, and tests.

## Implementation notes and public seam

Preserve `/api/series?generator&start&end`. Raise the maximum to 30 days. Emit an aligned half-open five-minute grid with nullable power/energy/value. Add deterministic preset/navigation helpers, AbortController handling, selected-range labels, null chart gaps, and long-range symbol suppression.

## TDD/test plan

First failing test: database API accepts exactly 30 days and rejects one second more. Then RED→GREEN gap-grid/coverage behavior. Then RED→GREEN deterministic range navigation and source/UI chart contracts. Run focused checks after each cycle, then broader suites.

## Verification command

`cd app/backend && python -m unittest tests.test_api tests.test_api_database_series && cd ../frontend && npm test && npm run typecheck && npm run build && cd ../.. && git diff --check`

Broader backend: `cd app/backend && python -m unittest discover -s tests -p 'test_*.py'`.

## Forbidden side effects

No commit, push, fetch, merge, PR, package installation, network access, deployment, service or database mutation, collector change, credential/.env access, or edits outside declared scope. Preserve accepted pre-existing frontend behavior.

## Acceptance evidence

Observed RED and GREEN focused tests, full backend/frontend checks, TypeScript/build, diff inspection, fresh independent read-only review, and reconciled Direct-Pi lifecycle.

## Status: pending
