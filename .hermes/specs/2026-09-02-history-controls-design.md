# BatteryWatch historical range controls design

## Outcome

Replace the fixed rolling 24-hour request with a bounded historical window that users can move backward and forward. Provide 24-hour, 7-day, and 30-day presets while preserving five-minute detail, browser-local timestamps, and visible data gaps.

## Design

- The backend `/api/series` seam remains explicit `start`/`end` ISO-8601 timestamps and accepts at most 30 days.
- Database responses emit each aligned five-minute timestamp in the requested half-open range. Missing power or price remains `null`; no imputation, interpolation, or forward-fill occurs.
- Coverage reports expected, observed, missing-power, missing-price, and both-missing interval counts. Summary energy/value uses only intervals with the required inputs.
- The frontend owns range state: 24h, 7d, or 30d duration plus an end timestamp. Previous/next shifts by one selected duration; next never moves beyond the current time; Latest returns to the rolling current window.
- Generator or range changes issue a new explicit request and abort the obsolete request. Server-range changes reset ECharts zoom to the full loaded window.
- The maximum rendered payload is bounded by the 30-day API limit. Five-minute detail is retained, but point symbols are disabled for 7/30-day ranges to avoid unnecessary rendering cost. Null power remains an ECharts gap (`connectNulls: false`).

## Rejected alternatives

- Client-only limit increase: rejected because sparse power rows visually bridge real feed gaps.
- Automatic historical backfill: rejected because database mutation, backup, and reconciliation are separate operator gates.
- Unbounded custom date range: rejected for this slice; presets plus window navigation satisfy the request without weakening the payload cap.

## Test seam

- FastAPI public contract tests prove 30-day acceptance, over-limit rejection, aligned null grid points, nullable estimate math, and coverage.
- Frontend Node contract/unit tests prove range calculation/navigation/request bounds and source-level UI/chart contracts.
- TypeScript, production build, full backend tests, and `git diff --check` are required.
