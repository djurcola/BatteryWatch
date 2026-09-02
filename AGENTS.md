# BatteryWatch agent guide

## Scope

Use this repository for the deployed BatteryWatch successor: a React/TypeScript frontend, FastAPI backend, PostgreSQL/TimescaleDB storage, and NEMWeb collectors/backfills. More specific guidance under `app/` overrides this file.

## Start here

1. Run `git status --short --branch`, `git worktree list`, and `git log -5 --oneline` before editing. Unknown or unrelated dirty state is a stop condition.
2. Read `app/deploy/README.md`, the relevant Python/TypeScript modules, and their tests before changing behavior.
3. Keep feature work in an isolated worktree. Do not edit or merge another lane implicitly.
4. Use the `djurcola-agent` fork/PR path for GitHub publication. Coder owns commits and publication; implementation workers do not receive credentials or push/deploy authority.

## Architecture and source ownership

- Frontend: `app/frontend/src`; production assets are built into `app/frontend/dist`.
- API and ingestion: `app/backend/batterywatch_api`.
- Database migrations: `app/migrations`, applied in numeric order by `app/deploy/migrate.sh`.
- Deployment/runbooks and systemd units: `app/deploy`.
- Battery identity/configuration: `app/config/battery_assets.json`.
- Production target: SSH user `dan` at `192.168.20.42` (`aemo-dashboard`) using `$HOME/.ssh/id_hermes`.
- Production releases are immutable directories under `$HOME/batterywatch/releases/<release-id>`. `$HOME/batterywatch/current` is the active symlink.
- Runtime configuration is host-local at `$HOME/.config/batterywatch/runtime.env`; never copy its values into Git, chat, logs, or command arguments.
- The API and collector are user-systemd services (`batterywatch-api.service` and `batterywatch-collector.service`). Use `systemctl --user`; do not guess a root service name.
- PostgreSQL is loopback-only. The runtime `batterywatch` role performs application DML but does not own all tables and cannot apply owner-only DDL.

## Credentials and privilege boundary

- Bitwarden Secrets Manager is the credential authority. The current Coder service account is read-only; it can retrieve approved secrets but cannot create aliases or update records.
- The legacy Bitwarden key `SUDO_PASSWORD` currently identifies `dan`'s sudo credential on `192.168.20.42`. Prefer future explicit aliases `192.168.20.42_USER` and `192.168.20.42_SUDO_PASSWORD` if a vault administrator creates them.
- Password values, access tokens, database URLs, Bitwarden project/record IDs, and secret metadata must never be written to `AGENTS.md`, source files, Git history, chat, or reusable shell history.
- Retrieve secrets just in time by key, keep them in an in-memory shell variable or `bws run` environment, pipe sudo input over stdin, and immediately `unset` variables. Never place a secret in a command-line argument or a persistent/temp file.
- Owner-only migrations require explicit sudo to the local `postgres` OS account, for example by piping the retrieved host sudo credential to remote `sudo -S -u postgres psql`. The application DSN remains sourced only from `runtime.env`.

## Verification commands

From the repository root:

- Backend: `PYTHONPATH=app/backend python3 -m unittest discover -s app/backend/tests -p 'test_*.py'`
- Frontend: `npm test -- --run` from `app/frontend`
- Frontend production build: `npm run build` from `app/frontend`
- API health after deployment: `curl --fail --silent http://127.0.0.1:8080/api/health`; require `data_mode=database`.

Run the focused tests first, then the complete relevant suites. A worker report or a stale earlier run is not acceptance evidence.

## Safe production deployment

1. Build and test from clean reviewed source.
2. Create a release tarball that excludes `.git`, caches, local env files, and credentials.
3. Copy it to `192.168.20.42`, extract into a new immutable `$HOME/batterywatch/releases/<release-id>`, install/update that release's venv as required, and build/include `app/frontend/dist`.
4. Create and verify a fresh database backup before migrations. `app/deploy/migrate.sh` deliberately requires a fresh verified backup manifest.
5. Apply all migrations, including `009_allow_archive_urls.sql`. Migration 009 permits exact archive interval receipt URLs shaped as `daily-archive.zip#nested-interval.zip`; an archive URL without the fragment is not an interval artifact identity.
6. Atomically repoint `$HOME/batterywatch/current`, then restart the user API and collector services.
7. Read back the symlink target, both service states/logs, API health, and representative bounded API queries. Roll back the symlink and services if a gate fails.

## Historical NEMWeb backfill

- Dispatch SCADA power and DispatchIS regional price are separate daily archive feeds containing 288 nested five-minute ZIPs. Treat them as separate operational passes so a parser/source failure in one feed cannot block the other.
- Use `python -m batterywatch_api.backfill_service` with one feed at a time: `--feeds power`, then `--feeds price`. Give each pass a unique run ID and the same bounded start/end.
- NEMWeb daily archives lag current reports; very recent report dates can return HTTP 404 until publication. Determine the newest published daily archive first and end the archive run at that boundary. The live collector should cover the newer overlap.
- Historical DispatchIS `PRICE_STATUS` values observed across the August 2026 archive range are `FIRM` and `NOT FIRM`; both are valid. Keep the parser fail-closed for unrecognized values and add evidence/tests before widening the allow-list.
- After each pass, query run/item/event ledgers and domain tables. Verify completed items, imported counts, min/max timestamps, five-minute continuity, overlap deduplication, and representative API history—not merely process exit status.

## Safety rules

- Do not expose PostgreSQL beyond loopback or change firewall/proxy routes for a migration.
- Do not infer SOC from Dispatch SCADA MW; SOC remains nullable unless authoritative data is present.
- Do not delete raw artifacts, ledger evidence, or failed runs to make a retry appear clean. Use a new run ID and retain provenance.
- Do not commit, push, merge, deploy, install, alter the database, or restart services without the applicable explicit gate and fresh verification.
