# BatteryWatch

BatteryWatch displays five-minute battery power and estimated energy value for NEM battery
assets.

> **This repository hosts the active BatteryWatch project** — the React/FastAPI/PostgreSQL
> successor under `app/`. The original static JavaScript app at the repository root
> (`index.html`, `BatteryData.json`) is **archived and no longer developed**; it remains in
> this repository for reference at tag `legacy-static-app`.

## Active project (successor)

- URL: <https://batterywatch.flow.danjurcola.com/>
- Source: `app/`
  - frontend: React/TypeScript (`app/frontend`), built assets in `app/frontend/dist`
  - backend: FastAPI (`app/backend/batterywatch_api`)
  - storage: PostgreSQL/TimescaleDB, migrations in `app/migrations`
  - deployment: systemd units and runbooks in `app/deploy`
- Runs on the `aemo-dashboard` VM (VM 430 on prox1, `192.168.20.42`) as user-systemd units
  (`batterywatch-api.service`, `batterywatch-collector.service`,
  `batterywatch-nextday.timer`), behind Pangolin/Traefik.
- Deployment procedure: [`app/deploy/README.md`](app/deploy/README.md).
- Agent guide: [`AGENTS.md`](AGENTS.md).

## Archived original (static app)

The original BatteryWatch is a static HTML/CSS/JavaScript app at the repository root
(`index.html`, `BatteryData.json`). It is **archived and no longer developed** — no
features, fixes, or data updates are made to it.

- Code: tag [`legacy-static-app`](https://github.com/djurcola/BatteryWatch/tree/legacy-static-app)
  (commit `466b49b`)
- URL: <https://batterywatch.danjurcola.com/> (Cloudflare Pages, unchanged)
- Data path: Cloudflare Pages → SWAG on VM2 ARM → backend API on the iGrid VM
  (`100.84.134.56:1886`) → InfluxDB. Public API routes:
  `https://backend.danjurcola.com/api/generators` and
  `https://backend.danjurcola.com/api/query`.
- The successor replaces it with its own API and PostgreSQL storage; the old InfluxDB
  backend is left running only so the archived page keeps working.

## Two URLs — which is which

| URL | What it is | Status |
|---|---|---|
| <https://batterywatch.flow.danjurcola.com/> | Successor app built from this repository's `app/` | **Active — developed here** |
| <https://batterywatch.danjurcola.com/> | Original static app from the repository root | **Archived — frozen, not developed** |

These are **separate deployments with separate backends**. `batterywatch.danjurcola.com` is
not a redirect and does not serve the successor. Use the `.flow.` hostname for the current
product.
