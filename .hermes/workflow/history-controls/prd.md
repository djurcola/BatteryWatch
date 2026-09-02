# PRD: Multi-day BatteryWatch history

## Problem and desired outcome

BatteryWatch always requests the most recent 24 hours, so users cannot inspect older windows or select a 7-day or 30-day history. Wider views must not imply that missing source observations exist.

## Users and scenarios

A BatteryWatch user selects 24h, 7d, or 30d; moves one window backward or forward; returns to Latest; and zooms/pans within the loaded window.

## Scope

- REQ-1: Offer 24h, 7d, and 30d range presets; default to latest 24h.
- REQ-2: Previous shifts back exactly one selected duration. Next shifts forward without ending after request time. Latest restores the rolling current window.
- REQ-3: Every request sends explicit ISO-8601 `start` and `end`; stale requests are cancelled and cannot overwrite current state.
- REQ-4: `/api/series` accepts ranges up to and including 30 days and rejects longer ranges before repository access.
- REQ-5: Database series returns the aligned five-minute grid for the half-open requested range with nullable power, price, energy, and value fields; no values are invented.
- REQ-6: Coverage exposes expected, observed-power, observed-price, missing-power, missing-price, and both-missing counts/percentages.
- REQ-7: Chart gaps remain visible, server-range changes reset zoom, and 7/30-day rendering avoids per-point symbols while retaining five-minute payload detail.
- REQ-8: Labels, empty states, summary labels, and displayed window describe the selected range rather than always saying 24 hours.

## Non-goals

- Running migrations, historical backfill, collector changes, service changes, deployment, or database writes.
- Unbounded/custom absolute ranges, server-side aggregates, or changing estimate formulas.
- Fabricating missing power, price, or SOC.

## Inputs, outputs, and failure behavior

Input is generator, range preset, and navigation action. Output is one explicit bounded API query and a chart for that window. Invalid or over-limit bounds return HTTP 400. Fetch errors retain a clear range-specific error. Obsolete requests are aborted without displaying an error.

## Architecture and data flow

`batterywatch_api.main` owns range validation, aligned grid construction, estimate math, and coverage. `models.py` owns nullable response types and additive coverage fields. A small frontend range module owns deterministic window transitions; `main.tsx` owns controls, fetching, and ECharts configuration. Dependencies continue from UI to the existing API seam and repository abstraction.

## Constraints

The API cap is 30 days. Times are stored/sent as UTC ISO-8601 and displayed in browser-local time. Existing frontend visual changes are accepted pre-existing baseline and must be preserved. No production or external side effects.

## Acceptance mapping

- REQ-1/2/3/8: frontend range unit/contract tests, TypeScript, production build.
- REQ-4: focused database API boundary tests.
- REQ-5/6: focused gap-aware FastAPI tests using in-memory repository records.
- REQ-7: frontend chart contract tests and production build.
- All: full backend suite, full frontend tests, `git diff --check`, independent final-source review.

## Slice map

One bounded vertical slice covers REQ-1 through REQ-8. This consciously combines the contract and UI because they share one small public seam and are not independently useful. Historical data population remains an operator-gated follow-up.

## Decisions and assumptions

Presets plus window navigation are the scrolling interaction. The 30-day maximum is also the raw-point cap. No unresolved material questions.
