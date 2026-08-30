"""Tests for the bounded historical backfill operator command."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
import json
from pathlib import Path
from typing import Any
import unittest

from batterywatch_api.backfill_ledger import (
    BackfillClaim,
    BackfillEnsureResult,
    BackfillRunFinalization,
    BackfillRunProgress,
)
from batterywatch_api.battery_assets import BatteryAsset
from batterywatch_api.historical_backfill import HistoricalBackfillResult
from batterywatch_api.historical_price_backfill import HistoricalPriceBackfillResult
from batterywatch_api.historical_scada_backfill import HistoricalScadaBackfillResult
from batterywatch_api.backfill_service import build_operator_plan, main

UTC = timezone.utc


class BackfillServiceTests(unittest.TestCase):
    def test_builds_canonical_ledger_plan_for_requested_feeds(self) -> None:
        start = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
        end = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)

        spec, items = build_operator_plan(
            "operator-run",
            start,
            end,
            feeds=("price", "power"),
            ingestion_version=7,
        )

        self.assertEqual((spec.run_id, spec.requested_start, spec.requested_end, spec.ingestion_version), ("operator-run", start, end, 7))
        self.assertEqual(
            tuple((item.feed, item.report_date, item.source_url) for item in items),
            (
                (
                    "dispatch_scada",
                    date(2026, 8, 29),
                    "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
                    "PUBLIC_DISPATCHSCADA_20260829.zip",
                ),
                (
                    "dispatch_price",
                    date(2026, 8, 29),
                    "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
                    "PUBLIC_DISPATCHIS_20260829.zip",
                ),
            ),
        )

    def test_main_outputs_deterministic_success_summary_without_database_url(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        captured: dict[str, Any] = {}
        start = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
        end = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
        scada_claim = BackfillClaim(
            "operator-run",
            "dispatch_scada",
            date(2026, 8, 29),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
            "PUBLIC_DISPATCHSCADA_20260829.zip",
            1,
        )
        price_claim = BackfillClaim(
            "operator-run",
            "dispatch_price",
            date(2026, 8, 29),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260829.zip",
            1,
        )
        progress = BackfillRunProgress("operator-run", "completed", 2, 0, 0, 2, 0, 2)
        result = HistoricalBackfillResult(
            BackfillEnsureResult(True, False, 2, 0),
            (scada_claim, price_claim),
            (HistoricalScadaBackfillResult(1, 100, 12, 0, False, False),),
            (HistoricalPriceBackfillResult(1, 5, 5, 1, True, False),),
            BackfillRunFinalization(False, progress),
        )

        def load_assets(path: Path) -> tuple[BatteryAsset, ...]:
            captured["assets_path"] = path
            return (
                BatteryAsset(
                    "BAT1",
                    "Battery One",
                    "NSW1",
                    10,
                    20,
                    "reviewed-registry",
                    datetime(2025, 3, 31, tzinfo=UTC),
                ),
            )

        def run(database_url: str, assets: Any, spec: Any, items: Any) -> HistoricalBackfillResult:
            captured.update(database_url=database_url, assets=tuple(assets), spec=spec, items=tuple(items))
            return result

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--run-id", "operator-run",
                    "--start", "2026-08-29T00:00:00Z",
                    "--end", "2026-08-29T10:00:00Z",
                    "--feeds", "power,price",
                    "--assets-path", "/tmp/reviewed-assets.json",
                ],
                environ={"BATTERYWATCH_DATABASE_URL": "postgresql://secret-value"},
                load_assets=load_assets,
                run=run,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["run_id"], "operator-run")
        self.assertEqual(payload["requested_start"], "2026-08-29T00:00:00Z")
        self.assertEqual(payload["requested_end"], "2026-08-29T10:00:00Z")
        self.assertEqual(payload["planned_item_count"], 2)
        self.assertEqual(payload["claimed_item_count"], 2)
        self.assertEqual(payload["scada_raw_observation_count"], 100)
        self.assertEqual(payload["scada_mapped_power_count"], 12)
        self.assertEqual(payload["price_source_record_count"], 5)
        self.assertEqual(payload["price_applied_record_count"], 5)
        self.assertEqual(payload["replayed_interval_count"], 1)
        self.assertEqual(payload["replayed_outer_artifact_count"], 1)
        self.assertEqual(payload["total_attempts"], 2)
        self.assertNotIn("secret-value", stdout.getvalue())
        self.assertEqual(captured["assets_path"], Path("/tmp/reviewed-assets.json"))

    def test_main_failure_reports_only_error_type_and_run_id(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        def fail_run(*args: Any, **kwargs: Any) -> HistoricalBackfillResult:
            raise RuntimeError("postgresql://secret-value private row")

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--run-id", "operator-run",
                    "--start", "2026-08-29T00:00:00Z",
                    "--end", "2026-08-29T10:00:00Z",
                ],
                environ={"BATTERYWATCH_DATABASE_URL": "postgresql://secret-value"},
                load_assets=lambda path: (),
                run=fail_run,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"error_type": "RuntimeError", "run_id": "operator-run", "status": "error"},
        )
        self.assertNotIn("secret-value", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
