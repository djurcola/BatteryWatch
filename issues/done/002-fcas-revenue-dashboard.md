# Slice: FR-2 Dashboard gross FCAS payment estimate

## Outcome

The existing FCAS panel displays the selected-window total and per-service estimated gross FCAS enablement payment, plus honest price/target coverage and interval price/payment detail, using only the FR-1 API contract.

## Requirements covered

REQ-FR-5; presentation portions of REQ-FR-4.

Authoritative design: `.hermes/specs/2026-09-04-fcas-revenue-estimate-design.md`. Read it and the completed FR-1 API models/tests before editing.

## Non-goals

- Any authoritative calculation in the browser.
- Backend/schema/ingestion changes.
- Actual settlement/profit language, FPP, cost recovery, contracts, taxes, fees, bids, or physical-response verification.
- New chart libraries, dependencies, routes, pages, or broad visual redesign.
- Deployment or generated production asset edits.

## Prerequisites

FR-1 accepted by Coder. Frontend dependencies are available through the existing untracked local `node_modules` symlink; do not install packages.

## Blocked by

FR-1.

## Blocks

None.

## Exclusive files/resources

- `app/frontend/src/main.tsx`
- `app/frontend/src/styles.css`
- `app/frontend/tests/fcas-contract.test.mjs`

Do not edit backend, migrations, package manifests/locks, or `app/frontend/dist`.

## Concurrency group

Sequential.

## Integration owner

Hermes Coder.

## Model-policy profile

implementation-default.

## Expected change surface

Three files, approximately 80–180 changed lines.

## Implementation notes and public seam

1. Use strict TDD: add one failing FCAS UI contract test, run it and confirm the expected failure, implement minimally, rerun, then repeat.
2. Read the actual FR-1 response models/tests; mirror that contract exactly in TypeScript. Do not invent or recalculate backend totals.
3. In the FCAS panel, show the selected-window total with the API's exact estimate label. Format finite AUD values with an existing or small locale-aware currency helper. A null/unavailable estimate must say unavailable—not `$0.00`. A true zero is `$0.00`.
4. Show target/price/payment coverage in understandable interval counts or percentages, clearly distinguishing partial from complete data.
5. Add each selected service's estimated payment and price/payment coverage to its existing summary card. Retain the existing target/availability status fields.
6. Add interval regional service price and estimated payment to the existing FCAS chart tooltip or an equivalently compact detail surface without adding a second chart. Preserve the target-MW line chart and gaps.
7. Preserve the exact physical-response disclaimer. Add concise text with the formula `target MW × regional FCAS price ÷ 12`, publication latency, and exclusions (settlement adjustments, FPP, cost recovery, contracts, fees, taxes). Do not call it actual revenue/profit.
8. Preserve selected-range requests, AbortController cancellation, loading/errors, publication-state badge, chart disposal/responsive behavior, and mobile usability.
9. Use semantic HTML and accessible status/labels. Keep visual changes consistent with current CSS.
10. Do not edit built artifacts; `npm run build` is verification only and `dist` is ignored/untracked by design.

## TDD/test plan

- First failing test: FCAS panel consumes and visibly labels the response-level estimated gross payment without browser-side arithmetic.
- Then test null versus true-zero presentation, coverage, per-service payment, formula/exclusions, tooltip/detail fields, and mobile/readability contract.
- Focused check: `node --test app/frontend/tests/fcas-contract.test.mjs` from repository root.
- Broader checks: complete frontend tests, TypeScript/build, then backend regression suite because the shared contract changed in FR-1.

Record RED and GREEN commands/results in your final report.

## Verification command

From repository root:

`node --test app/frontend/tests/fcas-contract.test.mjs && npm test -- --run --prefix app/frontend && npm run build --prefix app/frontend && PYTHONPATH=app/backend python3 -m unittest discover -s app/backend/tests -p 'test_*.py' && git diff --check`

## Forbidden side effects

Do not commit, push, merge, publish, deploy, install packages, access credentials, modify backend/schema/API behavior, run migrations, replay artifacts, restart services, edit generated `app/frontend/dist`, or handle another issue. Do not move this issue manually; the orchestrator owns issue movement.

## Acceptance evidence

- Contract tests prove estimate label/total, null versus zero, coverage, per-service display, formula/exclusions, and interval price/payment detail.
- Existing selected-range, cancellation, FCAS target chart, publication-state, disclaimer, and mobile contracts remain green.
- Full frontend tests, production build/typecheck, backend regression suite, and diff check pass with actual counts.
- Changed files and residual risks listed.

## Status

complete
