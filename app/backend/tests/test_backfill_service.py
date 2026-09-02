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
from batterywatch_api.historical_nextday_backfill import HistoricalNextDayBackfillResult
from batterywatch_api.historical_price_backfill import HistoricalPriceBackfillResult
from batterywatch_api.historical_scada_backfill import HistoricalScadaBackfillResult
from batterywatch_api.backfill_service import build_operator_plan, main
from batterywatch_api.backfill_service import build_operator_plan_details
from batterywatch_api.nextday_archives import (
    NEXTDAY_ARCHIVE_INDEX_URL,
    NextDayMonthlyArchiveRef,
)

UTC = timezone.utc


class BackfillServiceTests(unittest.TestCase):
    def test_builds_soc_plan_with_explicit_missing_official_months(self) -> None:
        def archive(month: int, size: int) -> NextDayMonthlyArchiveRef:
            filename = f"PUBLIC_NEXT_DAY_DISPATCH_2025{month:02d}01.zip"
            return NextDayMonthlyArchiveRef(
                date(2025, month, 1),
                filename,
                NEXTDAY_ARCHIVE_INDEX_URL + filename,
                size,
                datetime(2025, month + 1, 1, tzinfo=UTC),
            )

        details = build_operator_plan_details(
            "soc-operator-run",
            datetime(2025, 7, 15, tzinfo=UTC),
            datetime(2025, 9, 15, tzinfo=UTC),
            feeds=("soc",),
            ingestion_version=3,
            nextday_archives=(archive(7, 210_000_000), archive(8, 220_000_000)),
        )

        self.assertEqual(details.spec.run_id, "soc-operator-run")
        self.assertEqual(
            tuple((item.feed, item.report_date) for item in details.items),
            (("nextday_soc", date(2025, 7, 1)), ("nextday_soc", date(2025, 8, 1))),
        )
        self.assertEqual(details.missing_nextday_months, (date(2025, 9, 1),))

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
        soc_claim = BackfillClaim(
            "operator-run",
            "nextday_soc",
            date(2026, 8, 1),
            NEXTDAY_ARCHIVE_INDEX_URL + "PUBLIC_NEXT_DAY_DISPATCH_20260801.zip",
            1,
        )
        progress = BackfillRunProgress("operator-run", "completed", 3, 0, 0, 3, 0, 3)
        result = HistoricalBackfillResult(
            BackfillEnsureResult(True, False, 3, 0),
            (scada_claim, price_claim, soc_claim),
            (HistoricalScadaBackfillResult(1, 100, 12, 0, False, False),),
            (HistoricalPriceBackfillResult(1, 5, 5, 1, True, False),),
            BackfillRunFinalization(False, progress),
            (
                HistoricalNextDayBackfillResult(
                    "a" * 64,
                    False,
                    1,
                    0,
                    10,
                    8,
                    2,
                    10,
                    8,
                    2,
                    1,
                    7,
                ),
            ),
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

        filename = "PUBLIC_NEXT_DAY_DISPATCH_20260801.zip"
        nextday_reference = NextDayMonthlyArchiveRef(
            date(2026, 8, 1),
            filename,
            NEXTDAY_ARCHIVE_INDEX_URL + filename,
            220_000_000,
            datetime(2026, 9, 1, tzinfo=UTC),
        )

        def fetch_index(url: str, *, max_bytes: int) -> Any:
            captured.update(index_url=url, index_max_bytes=max_bytes)
            return type("Resource", (), {"body": b"official-index"})()

        def discover_nextday(payload: str, *, index_url: str) -> tuple[NextDayMonthlyArchiveRef, ...]:
            captured.update(index_payload=payload, discovery_index_url=index_url)
            return (nextday_reference,)

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--run-id", "operator-run",
                    "--start", "2026-08-29T00:00:00Z",
                    "--end", "2026-08-29T10:00:00Z",
                    "--feeds", "power,price,soc",
                    "--assets-path", "/tmp/reviewed-assets.json",
                ],
                environ={"BATTERYWATCH_DATABASE_URL": "postgresql://secret-value"},
                load_assets=load_assets,
                run=run,
                fetch_index=fetch_index,
                discover_nextday=discover_nextday,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["run_id"], "operator-run")
        self.assertEqual(payload["requested_start"], "2026-08-29T00:00:00Z")
        self.assertEqual(payload["requested_end"], "2026-08-29T10:00:00Z")
        self.assertEqual(payload["planned_item_count"], 3)
        self.assertEqual(payload["claimed_item_count"], 3)
        self.assertEqual(payload["scada_raw_observation_count"], 100)
        self.assertEqual(payload["scada_mapped_power_count"], 12)
        self.assertEqual(payload["price_source_record_count"], 5)
        self.assertEqual(payload["price_applied_record_count"], 5)
        self.assertEqual(payload["nextday_source_record_count"], 10)
        self.assertEqual(payload["nextday_applied_record_count"], 8)
        self.assertEqual(payload["nextday_null_count"], 1)
        self.assertEqual(payload["nextday_percentage_count"], 7)
        self.assertEqual(payload["missing_nextday_months"], [])
        self.assertEqual(payload["replayed_interval_count"], 3)
        self.assertEqual(payload["replayed_outer_artifact_count"], 1)
        self.assertEqual(payload["total_attempts"], 3)
        self.assertNotIn("secret-value", stdout.getvalue())
        self.assertEqual(captured["assets_path"], Path("/tmp/reviewed-assets.json"))
        self.assertEqual(captured["index_url"], NEXTDAY_ARCHIVE_INDEX_URL)
        self.assertEqual(captured["index_payload"], "official-index")
        self.assertEqual(captured["discovery_index_url"], NEXTDAY_ARCHIVE_INDEX_URL)
        self.assertGreater(captured["index_max_bytes"], 0)

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

    def test_main_reports_soc_source_unavailable_without_running_database_work(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        calls: list[str] = []
        filename = "PUBLIC_NEXT_DAY_DISPATCH_20260601.zip"
        last_available = NextDayMonthlyArchiveRef(
            date(2026, 6, 1),
            filename,
            NEXTDAY_ARCHIVE_INDEX_URL + filename,
            220_000_000,
            datetime(2026, 7, 1, tzinfo=UTC),
        )

        def fetch_index(url: str, *, max_bytes: int) -> Any:
            return type("Resource", (), {"body": b"official-index"})()

        def discover_nextday(
            payload: str,
            *,
            index_url: str,
        ) -> tuple[NextDayMonthlyArchiveRef, ...]:
            return (last_available,)

        def fail_load(path: Path) -> tuple[BatteryAsset, ...]:
            calls.append("load")
            raise AssertionError("source-unavailable run must not load assets")

        def fail_run(*args: Any, **kwargs: Any) -> HistoricalBackfillResult:
            calls.append("run")
            raise AssertionError("source-unavailable run must not open database work")

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--run-id",
                    "soc-missing-run",
                    "--start",
                    "2026-07-31T15:05:00Z",
                    "--end",
                    "2026-08-30T15:05:00Z",
                    "--feeds",
                    "soc",
                ],
                environ={"BATTERYWATCH_DATABASE_URL": "postgresql://secret-value"},
                load_assets=fail_load,
                run=fail_run,
                fetch_index=fetch_index,
                discover_nextday=discover_nextday,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(calls, [])
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "missing_nextday_months": ["2026-07-01", "2026-08-01"],
                "planned_item_count": 0,
                "requested_end": "2026-08-30T15:05:00Z",
                "requested_start": "2026-07-31T15:05:00Z",
                "run_id": "soc-missing-run",
                "status": "source_unavailable",
            },
        )
        self.assertNotIn("secret-value", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
