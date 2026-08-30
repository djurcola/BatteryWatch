"""Tests for deterministic supplied-plan historical backfill orchestration."""

from datetime import date, datetime, timezone
from typing import Any
import unittest

from batterywatch_api.backfill_ledger import (
    BackfillClaim,
    BackfillEnsureResult,
    BackfillPlanItem,
    BackfillRunFinalization,
    BackfillRunProgress,
    BackfillRunSpec,
)
from batterywatch_api.battery_assets import BatteryAsset
from batterywatch_api.historical_price_backfill import HistoricalPriceBackfillResult
from batterywatch_api.historical_nextday_backfill import HistoricalNextDayBackfillResult
from batterywatch_api.historical_scada_backfill import HistoricalScadaBackfillResult
from batterywatch_api.historical_backfill import run_historical_backfill

UTC = timezone.utc


class Connection:
    def __init__(self, number: int) -> None:
        self.number = number
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class HistoricalBackfillTests(unittest.TestCase):
    def test_ensures_claims_dispatches_and_finalizes_supplied_plan(self) -> None:
        connections: list[Connection] = []
        operations: list[str] = []
        start = datetime(2026, 8, 29, tzinfo=UTC)
        end = datetime(2026, 8, 30, tzinfo=UTC)
        spec = BackfillRunSpec("run-1", start, end, 1)
        scada_item = BackfillPlanItem(
            "dispatch_scada",
            date(2026, 8, 29),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
            "PUBLIC_DISPATCHSCADA_20260829.zip",
        )
        price_item = BackfillPlanItem(
            "dispatch_price",
            date(2026, 8, 29),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260829.zip",
        )
        soc_item = BackfillPlanItem(
            "nextday_soc",
            date(2026, 8, 1),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Next_Day_Dispatch/"
            "PUBLIC_NEXT_DAY_DISPATCH_20260801.zip",
        )
        scada_claim = BackfillClaim(
            spec.run_id,
            scada_item.feed,
            scada_item.report_date,
            scada_item.source_url,
            1,
        )
        price_claim = BackfillClaim(
            spec.run_id,
            price_item.feed,
            price_item.report_date,
            price_item.source_url,
            1,
        )
        soc_claim = BackfillClaim(
            spec.run_id,
            soc_item.feed,
            soc_item.report_date,
            soc_item.source_url,
            1,
        )
        claims = [scada_claim, price_claim, soc_claim, None]
        assets = (
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
        progress = BackfillRunProgress("run-1", "completed", 3, 0, 0, 3, 0, 3)
        finalization = BackfillRunFinalization(False, progress)

        def connect(database_url: str, *, connect_timeout: int) -> Connection:
            self.assertEqual((database_url, connect_timeout), ("postgresql://private", 10))
            connection = Connection(len(connections) + 1)
            connections.append(connection)
            return connection

        class Ledger:
            def __init__(self, connection: Connection) -> None:
                self.connection = connection

            def ensure_run(self, actual_spec: BackfillRunSpec, items: Any) -> BackfillEnsureResult:
                self.assert_equal(actual_spec, spec)
                self.assert_equal(tuple(items), (scada_item, price_item, soc_item))
                operations.append(f"ensure:{self.connection.number}")
                return BackfillEnsureResult(True, False, 3, 0)

            def claim_next(self, run_id: str) -> BackfillClaim | None:
                self.assert_equal(run_id, spec.run_id)
                operations.append(f"claim:{self.connection.number}")
                return claims.pop(0)

            def finalize(self, run_id: str) -> BackfillRunFinalization:
                self.assert_equal(run_id, spec.run_id)
                operations.append(f"finalize:{self.connection.number}")
                return finalization

            def assert_equal(self, actual: Any, expected: Any) -> None:
                if actual != expected:
                    raise AssertionError(f"{actual!r} != {expected!r}")

        def run_scada(
            database_url: str,
            actual_assets: Any,
            claim: BackfillClaim,
            range_start: datetime,
            range_end: datetime,
        ) -> HistoricalScadaBackfillResult:
            self.assertEqual((database_url, tuple(actual_assets), claim, range_start, range_end), ("postgresql://private", assets, scada_claim, start, end))
            operations.append("scada")
            return HistoricalScadaBackfillResult(2, 20, 4, 1, True, False)

        def run_price(
            database_url: str,
            claim: BackfillClaim,
            range_start: datetime,
            range_end: datetime,
        ) -> HistoricalPriceBackfillResult:
            self.assertEqual((database_url, claim, range_start, range_end), ("postgresql://private", price_claim, start, end))
            operations.append("price")
            return HistoricalPriceBackfillResult(2, 10, 5, 0, False, False)

        def run_soc(
            database_url: str,
            actual_assets: Any,
            claim: BackfillClaim,
            range_start: datetime,
            range_end: datetime,
            ingestion_version: int,
        ) -> HistoricalNextDayBackfillResult:
            self.assertEqual(
                (
                    database_url,
                    tuple(actual_assets),
                    claim,
                    range_start,
                    range_end,
                    ingestion_version,
                ),
                ("postgresql://private", assets, soc_claim, start, end, 1),
            )
            operations.append("soc")
            return HistoricalNextDayBackfillResult(
                "a" * 64,
                False,
                2,
                0,
                20,
                18,
                2,
                20,
                18,
                2,
                3,
                15,
            )

        result = run_historical_backfill(
            "postgresql://private",
            assets,
            spec,
            (scada_item, price_item, soc_item),
            connect=connect,
            ledger_factory=Ledger,
            run_scada=run_scada,
            run_price=run_price,
            run_nextday=run_soc,
        )

        self.assertEqual(result.ensure_result, BackfillEnsureResult(True, False, 3, 0))
        self.assertEqual(result.claims, (scada_claim, price_claim, soc_claim))
        self.assertEqual(result.scada_results, (HistoricalScadaBackfillResult(2, 20, 4, 1, True, False),))
        self.assertEqual(result.price_results, (HistoricalPriceBackfillResult(2, 10, 5, 0, False, False),))
        self.assertEqual(
            result.nextday_results,
            (
                HistoricalNextDayBackfillResult(
                    "a" * 64,
                    False,
                    2,
                    0,
                    20,
                    18,
                    2,
                    20,
                    18,
                    2,
                    3,
                    15,
                ),
            ),
        )
        self.assertEqual(result.finalization, finalization)
        self.assertEqual(result.claimed_count, 3)
        self.assertEqual(result.scada_raw_observation_count, 20)
        self.assertEqual(result.scada_mapped_power_count, 4)
        self.assertEqual(result.price_source_record_count, 10)
        self.assertEqual(result.price_applied_record_count, 5)
        self.assertEqual(result.nextday_source_record_count, 20)
        self.assertEqual(result.nextday_applied_record_count, 18)
        self.assertEqual(result.nextday_null_count, 3)
        self.assertEqual(result.nextday_percentage_count, 15)
        self.assertEqual(result.replayed_interval_count, 3)
        self.assertEqual(result.replayed_outer_artifact_count, 1)
        self.assertEqual(
            operations,
            [
                "ensure:1",
                "claim:2",
                "scada",
                "claim:3",
                "price",
                "claim:4",
                "soc",
                "claim:5",
                "finalize:6",
            ],
        )
        self.assertEqual(
            [connection.close_calls for connection in connections],
            [1, 1, 1, 1, 1, 1],
        )

    def test_runner_failure_stops_without_finalization_and_preserves_error(self) -> None:
        connections: list[Connection] = []
        failure = RuntimeError("private database detail")
        start = datetime(2026, 8, 29, tzinfo=UTC)
        end = datetime(2026, 8, 30, tzinfo=UTC)
        spec = BackfillRunSpec("run-2", start, end, 1)
        item = BackfillPlanItem(
            "dispatch_scada",
            date(2026, 8, 29),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
            "PUBLIC_DISPATCHSCADA_20260829.zip",
        )
        claim = BackfillClaim(spec.run_id, item.feed, item.report_date, item.source_url, 1)
        claims = [claim]

        def connect(unused_url: str, *, connect_timeout: int) -> Connection:
            connection = Connection(len(connections) + 1)
            connections.append(connection)
            return connection

        class Ledger:
            def __init__(self, connection: Connection) -> None:
                self.connection = connection

            def ensure_run(self, actual_spec: BackfillRunSpec, items: Any) -> BackfillEnsureResult:
                return BackfillEnsureResult(True, False, 1, 0)

            def claim_next(self, run_id: str) -> BackfillClaim | None:
                return claims.pop(0)

            def finalize(self, run_id: str) -> BackfillRunFinalization:
                raise AssertionError("failed invocation must not finalize")

        def fail_scada(*args: Any, **kwargs: Any) -> HistoricalScadaBackfillResult:
            raise failure

        with self.assertRaises(RuntimeError) as raised:
            run_historical_backfill(
                "postgresql://private",
                (),
                spec,
                (item,),
                connect=connect,
                ledger_factory=Ledger,
                run_scada=fail_scada,
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual([connection.close_calls for connection in connections], [1, 1])

    def test_claim_count_is_bounded_by_supplied_plan(self) -> None:
        start = datetime(2026, 8, 29, tzinfo=UTC)
        end = datetime(2026, 8, 30, tzinfo=UTC)
        spec = BackfillRunSpec("run-3", start, end, 1)
        item = BackfillPlanItem(
            "dispatch_scada",
            date(2026, 8, 29),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
            "PUBLIC_DISPATCHSCADA_20260829.zip",
        )
        claim = BackfillClaim(spec.run_id, item.feed, item.report_date, item.source_url, 1)
        duplicate_claims = [claim, claim, None]
        run_calls: list[BackfillClaim] = []

        class Ledger:
            def __init__(self, connection: Connection) -> None:
                self.connection = connection

            def ensure_run(self, actual_spec: BackfillRunSpec, items: Any) -> BackfillEnsureResult:
                return BackfillEnsureResult(True, False, 1, 0)

            def claim_next(self, run_id: str) -> BackfillClaim | None:
                return duplicate_claims.pop(0)

            def finalize(self, run_id: str) -> BackfillRunFinalization:
                raise AssertionError("over-claimed invocation must not finalize")

        def run_scada(*args: Any, **kwargs: Any) -> HistoricalScadaBackfillResult:
            run_calls.append(args[2])
            return HistoricalScadaBackfillResult(1, 1, 1, 0, False, False)

        next_connection = 0

        def connect(unused_url: str, *, connect_timeout: int) -> Connection:
            nonlocal next_connection
            next_connection += 1
            return Connection(next_connection)

        with self.assertRaisesRegex(ValueError, "claim limit"):
            run_historical_backfill(
                "postgresql://private",
                (),
                spec,
                (item,),
                connect=connect,
                ledger_factory=Ledger,
                run_scada=run_scada,
            )

        self.assertEqual(run_calls, [claim])


if __name__ == "__main__":
    unittest.main()
