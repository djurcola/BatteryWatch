# FCAS gross enablement payment estimate

## Problem and desired outcome

BatteryWatch shows finalized per-DUID FCAS targets but not their monetary value. Add a transparent estimate of the gross FCAS provider payment for each service and selected time window, using public AEMO dispatch outputs already retained by BatteryWatch.

## Users and scenarios

A dashboard user selects a battery and a bounded history range, then sees per-service and total estimated FCAS enablement payment alongside target MW. Missing target or price data remains explicit and never becomes a fabricated zero.

## Scope

- Parse the ten unrounded regional FCAS RRP fields and matching APC flags from the existing `DISPATCH,PRICE` v5 row in retained DispatchIS artifacts.
- Persist one grouped FCAS-price map on each effective `nem_price_5m` region/interval row.
- Join finalized per-DUID FCAS target MW to its region's same-interval service price.
- Calculate each five-minute estimate as `target_mw × regional_price_aud_per_mw_hour ÷ 12`.
- Return per-point price/payment, per-service totals, total payment, and honest target/price coverage from the bounded `/api/fcas` endpoint.
- Display the estimate and coverage in the existing FCAS dashboard panel.
- Permit exact retained-artifact replay to fill a missing additive FCAS-price projection without weakening immutable artifact conflict checks or correction precedence.

## Non-goals

- Actual participant settlement statements, net profit, bids/offers, FCAS cost recovery, Frequency Performance Payments, contracts, taxes, fees, losses, degradation, auxiliary load, or proof of physical response.
- A new downloader or external price feed.
- A production migration, historical replay, deployment, service restart, commit, push, PR, or merge in the implementation slices.
- Changing the existing energy-value estimate.

## Requirements

### REQ-FR-1 — Authoritative price projection

The strict DispatchIS v5 parser reads all ten service RRP values and service APC flags from the already-validated `DISPATCH,PRICE` row. Values are finite, non-negative or null; flags are non-negative integers. Unknown, malformed, duplicate, missing-schema, or non-finite inputs fail closed. The energy RRP behavior remains unchanged.

### REQ-FR-2 — Additive persistence and replay

A numbered additive migration adds a validated grouped FCAS-price projection to `nem_price_5m`. Existing rows may remain unpriced until replay. New ingestion persists the projection. Exact immutable artifact replay may populate a previously missing projection only when the stored energy-price row and provenance match; it must not overwrite a populated projection, accept conflicting bytes, regress a newer correction, or turn a conflict into a replay.

### REQ-FR-3 — Bounded estimate API

`GET /api/fcas` keeps its existing 30-day and service-filter bounds and joins only the requested generator's region, selected interval, and selected services. For every service point it returns nullable unrounded regional price and nullable estimated gross payment. Payment is present only when both target and price are present. Zero target or zero price produces a real zero. Per-service summaries and the response include payment totals and target/price coverage sufficient to distinguish complete, partial, and unavailable estimates.

### REQ-FR-4 — Honest semantics

The API and UI call this an **estimated gross FCAS enablement payment**, not actual revenue or profit. They state the formula and exclude settlement adjustments, FPP, cost recovery, contracts, fees, taxes, and verified physical response. Existing Next Day publication latency and finalized-target disclaimer remain visible.

### REQ-FR-5 — Dashboard presentation

The FCAS panel shows selected-window total estimated payment, per-service estimated payment, price/target coverage, and service prices/payments in a readable chart tooltip or summary. Null data displays as unavailable rather than `$0`. Requests retain the existing cancellation and selected-range lifecycle. Mobile layout remains usable.

## Inputs, outputs, and failure behavior

- Input target: effective `generator_fcas_5m.fcas_services[service].target_mw`.
- Input price: effective same-interval `nem_price_5m` grouped FCAS-price RRP for the generator region.
- Output unit: AUD per five-minute interval and summed AUD for the requested half-open range.
- Price unit label: AUD/MW/hour, matching AEMO's payment formula.
- A missing target or price yields `null` payment and increments missing coverage.
- Malformed stored maps or repository failures produce the existing generic FCAS 503 without leaking storage details.
- Market suspension and APC flags remain provenance/quality metadata; the post-adjustment RRP is used because AEMO identifies the unrounded `DISPATCHPRICE` value as the settlement input.

## Architecture and seam

`aemo.py` owns strict wire parsing. `storage.py` owns typed grouped regional FCAS prices and bounded reads. The existing DispatchIS ingestion owns additive persistence/replay. `main.py` owns the pure interval join and estimate response contract. `main.tsx` consumes only the API contract; it does not calculate authoritative totals independently.

Dependencies continue inward from collector/backfill adapters to parser/storage, then API to storage and frontend to API. No second FCAS downloader, revenue table, or browser-side source-of-truth calculation is introduced.

## Acceptance evidence

- Focused parser/storage/ingestion/API tests prove all ten mappings, null/zero/non-finite behavior, exact formula, partial coverage, generic 503, service filtering, and safe additive replay.
- Migration contract tests prove migration ordering and grouped-map validation.
- Full backend suite passes with Pyright reporting zero errors/warnings.
- Frontend FCAS contract tests, full frontend tests, TypeScript typecheck, production build, and `git diff --check` pass.
- A fresh read-only review of the final source finds no material issue.
- Production activation remains a separate explicit gate requiring backup, migration, bounded retained-artifact replay for the FCAS-covered dates, service continuity, and exact API/UI read-back.

## Vertical slices

1. **FR-1 Backend estimate contract:** parser, typed grouped persistence, replay-safe projection, and `/api/fcas` estimate response.
2. **FR-2 Dashboard estimate:** render total/per-service estimate and coverage from the API without duplicating settlement logic.

FR-2 is blocked by FR-1 because it consumes the settled API contract.

## Decisions and evidence

- **Chosen:** estimate gross provider enablement payment from target MW and unrounded regional DispatchPrice FCAS RRP. This is the narrowest public-data calculation that directly follows AEMO's provider-payment formula.
- **Rejected:** call it actual revenue or net profit. Public outputs do not reproduce a participant settlement statement and do not include FPP or participant-specific commercial adjustments.
- **Rejected:** ingest a second price feed. BatteryWatch already retains the canonical DispatchIS artifacts containing the ten FCAS prices.
- **Assumption:** the user's request authorizes autonomous local implementation and verification, but not commit/publication or production activation.

Official evidence retrieved 2026-09-04:

- AEMO, *Guide to Frequency Control Ancillary Services*, v4.0 (17 September 2025): provider payment is `MWE × CP / 12`; bids and clearing prices are dollars per MW per hour. https://www.aemo.com.au/-/media/files/electricity/nem/security_and_reliability/ancillary_services/guide-to-frequency-control-ancillary-services.pdf?rev=48b25122c2384d67b69046cbdb56bacb
- AEMO, *Settlements Guide to Ancillary Services and Frequency Performance Payments*, v7.2 (2 December 2025): use the unrounded ancillary service price from `DISPATCHPRICE`, not rounded `TRADINGPRICE`. https://www.aemo.com.au/-/media/files/electricity/nem/data/ancillary_services/2025/settlements-guide-to-ancillary-services-and-frequency-performance-payments.pdf?rev=e7c4fb3fc63347fca83cd07fa283f9c0&sc_lang=en
- AEMO MMS Data Model, `DISPATCHPRICE`: public five-minute energy and FCAS prices, including all ten service RRP/APC fields. https://visualisations.aemo.com.au/aemo/nemweb/MMSDataModelReport/Electricity/MMS%20Data%20Model%20Report_files/MMS_130.htm
- AEMO, *Guide to Analysing Constraint Outcomes for FCAS Cost Recovery* (8 June 2025): FCAS prices come from the pricing run and enablement from the physical intervention run. https://www.aemo.com.au/-/media/files/initiatives/frequency-performance-payments-project/aemo-guide-to-constraint-outcomes-for-fcas-cost-recovery.pdf?la=en

## Open questions

None for local implementation. Production replay range and activation timing are operational choices gated after reviewed code is ready.
