"""Contract tests for the one-shot Next Day CURRENT operator service."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
import json
from io import StringIO
from pathlib import Path
import unittest

from batterywatch_api.battery_assets import BatteryAsset
from batterywatch_api.nextday_current_collection import (
    NextDayCurrentArtifactFailure,
    NextDayCurrentCollectionResult,
    NextDayCurrentProjectionTotals,
)
from batterywatch_api.nextday_current_service import main


ROOT = Path(__file__).resolve().parents[2]


def _totals(**overrides: int) -> NextDayCurrentProjectionTotals:
    values = {
        "attempted": 1,
        "completed": 1,
        "replayed": 0,
        "failed": 0,
        "remaining": 1,
        "source_rows": 10,
        "raw_inserted": 10,
        "raw_replayed": 0,
        "effective_candidates": 10,
        "effective_applied": 10,
        "effective_replayed": 0,
        "source_null_count": 1,
        "percentage_count": 9,
        "reported_service_count": 0,
    }
    values.update(overrides)
    return NextDayCurrentProjectionTotals(**values)


def _result(
    *,
    failed: int = 0,
    failures: tuple[NextDayCurrentArtifactFailure, ...] = (),
) -> NextDayCurrentCollectionResult:
    completed_dates = () if failed else (date(2026, 9, 1),)
    return NextDayCurrentCollectionResult(
        attempted=1,
        completed=len(completed_dates),
        replayed=0,
        failed=failed,
        remaining=1 if failed else 0,
        completed_report_dates=completed_dates,
        min_completed_report_date=(completed_dates[0] if completed_dates else None),
        max_completed_report_date=(completed_dates[-1] if completed_dates else None),
        failures=failures,
        soc=_totals(failed=failed),
        fcas=_totals(failed=failed),
    )


class NextDayCurrentServiceTests(unittest.TestCase):
    def test_main_injects_environment_assets_and_window_and_serializes_result(self) -> None:
        captured: dict[str, object] = {}
        stdout = StringIO()
        stderr = StringIO()
        reviewed_asset = BatteryAsset(
            "ADPBA1",
            "Adelaide Power Battery",
            "SA1",
            10.0,
            12.6,
            "reviewed",
            datetime(2025, 3, 31, tzinfo=timezone.utc),
        )

        def load_assets(path: Path):
            captured["assets_path"] = path
            return (reviewed_asset,)

        def run(database_url: str, assets, **kwargs):
            captured.update(
                database_url=database_url,
                assets=tuple(assets),
                kwargs=kwargs,
            )
            return _result()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--assets-path",
                    "/reviewed/assets.json",
                    "--limit",
                    "1",
                    "--start-date",
                    "2026-09-01",
                    "--end-date",
                    "2026-09-03",
                ],
                environ={
                    "BATTERYWATCH_DATABASE_URL": "postgresql://secret-value",
                    "BATTERYWATCH_ASSETS_PATH": "/unreviewed/assets.json",
                },
                load_assets=load_assets,
                run=run,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(captured["assets_path"], Path("/reviewed/assets.json"))
        self.assertEqual(captured["database_url"], "postgresql://secret-value")
        self.assertEqual(captured["assets"], (reviewed_asset,))
        self.assertEqual(captured["kwargs"], {
            "limit": 1,
            "start_date": date(2026, 9, 1),
            "end_date": date(2026, 9, 3),
        })
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            (payload["attempted"], payload["completed"], payload["replayed"],
             payload["failed"], payload["remaining"]),
            (1, 1, 0, 0, 0),
        )
        self.assertEqual(payload["completed_report_dates"], ["2026-09-01"])
        self.assertEqual(payload["min_completed_report_date"], "2026-09-01")
        self.assertEqual(payload["max_completed_report_date"], "2026-09-01")
        self.assertEqual(payload["failures"], [])
        self.assertEqual(payload["soc"]["source_rows"], 10)
        self.assertEqual(payload["soc"]["percentage_count"], 9)
        self.assertEqual(payload["fcas"]["source_rows"], 10)
        self.assertEqual(payload["fcas"]["reported_service_count"], 0)
        self.assertNotIn("secret-value", stdout.getvalue())

    def test_main_returns_one_for_cycle_failures_and_redacts_failure_evidence(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        result = _result(
            failed=1,
            failures=(
                NextDayCurrentArtifactFailure(
                    date(2026, 9, 2),
                    "RuntimeError: postgresql://secret-value",
                    "https://secret.example/artifact",
                ),
            ),
        )

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [],
                environ={"BATTERYWATCH_DATABASE_URL": "postgresql://secret-value"},
                load_assets=lambda path: (),
                run=lambda *args, **kwargs: result,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["failures"], [{
            "report_date": "2026-09-02",
            "error_type": "Error",
            "location": "unknown",
        }])
        self.assertNotIn("secret-value", stdout.getvalue())

    def test_main_reports_runtime_failure_without_exception_text_or_credentials(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        def fail_run(*args, **kwargs):
            raise RuntimeError("postgresql://secret-value private details")

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [],
                environ={"BATTERYWATCH_DATABASE_URL": "postgresql://secret-value"},
                load_assets=lambda path: (),
                run=fail_run,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"error_type": "RuntimeError", "status": "error"},
        )
        self.assertNotIn("secret-value", stderr.getvalue())
        self.assertNotIn("private details", stderr.getvalue())

    def test_argparse_rejects_unpaired_non_iso_and_out_of_range_arguments(self) -> None:
        for argv in (
            ["--limit", "3"],
            ["--start-date", "2026-09-01"],
            ["--start-date", "2026-09-01", "--end-date", "20260903"],
            ["--start-date", "2026-09-03", "--end-date", "2026-09-01"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as raised:
                    main(argv, environ={})
                self.assertEqual(raised.exception.code, 2)


class NextDayDeploymentContractTests(unittest.TestCase):
    def test_nextday_service_is_a_hardened_release_bound_oneshot(self) -> None:
        unit = (ROOT / "deploy" / "batterywatch-nextday.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("Type=oneshot", unit)
        self.assertIn("After=network-online.target", unit)
        self.assertIn("EnvironmentFile=%h/.config/batterywatch/runtime.env", unit)
        self.assertIn(
            "ExecStart=%h/batterywatch/current/venv/bin/python -m "
            "batterywatch_api.nextday_current_service --assets-path "
            "%h/batterywatch/current/app/config/battery_assets.json --limit 1",
            unit,
        )
        self.assertIn("WorkingDirectory=%h/batterywatch/current/app/backend", unit)
        self.assertIn("TimeoutStartSec=300s", unit)
        for hardening in (
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
            "MemoryDenyWriteExecute=true",
        ):
            with self.subTest(hardening=hardening):
                self.assertIn(hardening, unit)
        self.assertNotIn("batterywatch-collector.service", unit)

    def test_nextday_timer_is_daily_persistent_randomized_and_targets_oneshot(self) -> None:
        timer = (ROOT / "deploy" / "batterywatch-nextday.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("OnCalendar=*-*-* 21:00:00 UTC", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=5min", timer)
        self.assertIn("Unit=batterywatch-nextday.service", timer)
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
