# BatteryWatch activation runbook

The collector runs at a 180-second (three-minute) cadence. Each poll enumerates
all canonical CURRENT Dispatch SCADA and DispatchIS references, reconciles them
in the durable per-file ledger, and processes a bounded fair oldest/newest
claim batch independently for each feed; it is not a latest-only poll.

New battery DUIDs are not automatically admitted. See
[`../docs/generator-onboarding.md`](../docs/generator-onboarding.md) for the
reviewed-registry boundary, unknown-DUID behavior, onboarding procedure, and
historical-data implications.

## Daily Next Day CURRENT slice

The separate `batterywatch-nextday.timer` runs the one-shot
`batterywatch-nextday.service` daily at 21:00 UTC (07:00 AEST), with a bounded
five-minute randomized delay and persistent catch-up after downtime. It invokes
`batterywatch_api.nextday_current_service` with the reviewed release asset map
and `--limit 1`, so each scheduled run processes only the newest incomplete
Next Day CURRENT artifact. The service is inactive between successful runs;
the timer is the unit that remains enabled and active. This path is independent
of the three-minute SCADA/price collector and does not drain its CURRENT backlog.

Install the units for the active immutable release with the user systemd
manager, then enable the timer:

```sh
mkdir -p ~/.config/systemd/user
cp ~/batterywatch/current/app/deploy/batterywatch-nextday.service \
   ~/batterywatch/current/app/deploy/batterywatch-nextday.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now batterywatch-nextday.timer
systemctl --user is-active batterywatch-nextday.timer
# After a successful invocation, this should be inactive:
systemctl --user is-active batterywatch-nextday.service
```

For a bounded operator backfill, supply both inclusive report dates:

```sh
python -m batterywatch_api.nextday_current_service \
  --assets-path ~/batterywatch/current/app/config/battery_assets.json \
  --limit 2 --start-date 2026-09-01 --end-date 2026-09-03
```

## Fixed safety boundary

- PostgreSQL/TimescaleDB remains loopback-only; do not change listeners, firewall, or proxy routes.
- Keep the current fixture API available until backup, restore, migration, collector, API, dashboard, and rollback gates pass.
- The reviewed battery map contains 64 unique current DUIDs. The 35 additions, including `HPR1`, come from the AEMO NEM Registration and Exemption List published 2026-08-25. Five rows remain explicitly excluded because they are not observable in current Dispatch SCADA or have zero/unknown registered storage capacity; do not guess missing capacity.
- SOC remains nullable. Dispatch SCADA MW is public power telemetry, not SOC.
- Do not commit connection strings or copy them into logs/chat.

## Prerequisites

1. An approved least-privilege local PostgreSQL role/DSN capable of schema creation and DML on the BatteryWatch database.
2. Non-interactive authority to stop/replace the existing root-managed `batterywatch.service` on port 8080, or an administrator performs that exact cutover locally.
3. `pg_dump`, `pg_restore`, and `psql` available on the target.
4. A release installed at `%h/batterywatch/releases/<release-id>` with `%h/batterywatch/current` as the reviewed symlink, a venv containing `app/backend/requirements.txt`, and a built `app/frontend/dist`.

## Gates in order

1. Record current API health and unit state; confirm response says `data_mode=fixture`.
2. Create a fresh custom-format backup with `backup-verify.sh`; verify its checksum and `pg_restore --list`.
3. Restore that backup into an isolated temporary database, run the engine integrity checks, and prove row-count parity for existing domain tables. Drop the temporary database only after recording redacted parity evidence.
4. Export `BATTERYWATCH_BACKUP_MANIFEST` as the fresh `.sha256` path from step 2, then run `migrate.sh`; it rechecks checksum and age before verifying sixteen application tables, including immutable DispatchIS price artifacts and the per-file CURRENT artifact ledger.
5. Run one collector cycle from the candidate release with `python -m batterywatch_api.collector_service --once`.
6. Query `verify-live.sql` plus `dispatch_price_artifacts`/`nem_price_5m`; require fresh SCADA and DispatchIS artifacts, raw power observations, mapped `generator_power_5m` rows, exactly five current regional price rows, and at least one reviewed battery DUID.
7. Install the two user units under `~/.config/systemd/user/`, reload the user manager, enable/start the collector, and verify two successive five-minute artifacts or an exact replay followed by a new artifact.
8. Stop only the approved root-managed fixture API, start the user `batterywatch-api.service` on port 8080, and require `/api/health` to report `data_mode=database`.
9. Verify `/api/generators`, a bounded `/api/series` request, and the browser chart for one battery. Confirm regional price coverage/value estimates are visible, SOC null coverage remains explicit, and no fixture DUID appears.

## Host-side rollback

1. Stop the user `batterywatch-api.service` and `batterywatch-collector.service`.
2. Restart the preserved root-managed fixture `batterywatch.service`.
3. Verify port 8080 and `/api/health` return `data_mode=fixture`.
4. Leave additive raw/effective rows and migration tables intact for evidence; do not drop data during rollback.
5. Point `%h/batterywatch/current` back to the previous immutable release only after service rollback is healthy.
