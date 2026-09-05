# Issue 003 — Collapsible FCAS service details and battery favicon

Status: complete

## Goal

Restore the BatteryWatch battery tab icon and make the complete per-service FCAS summary-card grid beneath the FCAS target graph collapsible without hiding the graph or its estimate/coverage context.

## Requirements

1. Wrap all ten FCAS service summary cards in one accessible collapsible control immediately below the graph.
2. Preserve the current expanded presentation by default so existing information is not silently hidden; users can collapse and re-expand it.
3. Give the control a clear visible label such as `FCAS service details`, an obvious disclosure indicator, keyboard/native accessibility, and responsive styling consistent with the existing dashboard.
4. Keep the graph, estimate, disclaimer, coverage, and formula outside the collapsible region.
5. Restore the browser-tab battery icon using the previous app's battery favicon design (`🔋` in an SVG data favicon) or an equivalent local SVG battery icon. Do not depend on a network asset.
6. Add or update behavior-focused frontend contract tests for the collapse boundary/default and favicon link.

## Non-goals

- Do not change FCAS calculations, API contracts, chart behavior, or backend code.
- Do not redesign individual service cards.
- Do not alter generator onboarding documentation or deployment runbooks.

## Likely files

- `app/frontend/src/main.tsx`
- `app/frontend/src/styles.css`
- `app/frontend/index.html`
- `app/frontend/tests/fcas-contract.test.mjs` and/or a focused favicon contract test

## Acceptance checks

From `app/frontend`:

- `npm test -- --run`
- `npm run typecheck`
- `npm run build`

From repository root:

- `git diff --check`
- Inspect built `dist/index.html` to confirm it contains the favicon declaration.

## Boundaries

Implement and test only this issue. Do not commit, push, merge, publish, deploy, install packages, restart services, or modify any other issue. Coder owns review, Git, release packaging, and deployment.
