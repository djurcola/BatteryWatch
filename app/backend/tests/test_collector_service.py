"""Tests for the separate Dispatch SCADA collector runtime."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

from batterywatch_api.battery_assets import BatteryAsset
from batterywatch_api.collector import DispatchScadaCollection
from batterywatch_api.collector_service import (
    CollectorCycleCounters,
    CollectorCycleResult,
    CollectorFeedCounters,
    main,
    run_collection_cycle,
    run_current_collection_cycle,
    run_database_cycle,
    run_price_collection_cycle,
    run_polling_loop,
)
from batterywatch_api.collector_ledger import (
    CurrentArtifactClaim,
    CurrentArtifactLedgerReconciliation,
    CurrentArtifactReference,
)
from batterywatch_api.dispatch_price_ingestion import DispatchPriceIngestionResult
from batterywatch_api.dispatch_scada_ingestion import DispatchScadaIngestionResult
from batterywatch_api.nemweb_dispatch_prices import (
    DispatchPriceArtifact,
    DispatchPriceArtifactRef,
    DispatchPriceCollection,
)
from batterywatch_api.nemweb_dispatch_scada import (
    DispatchScadaArtifact,
    DispatchScadaArtifactRef,
)
from batterywatch_api.storage import GeneratorPower5m, RegionalPrice5m


UTC = timezone.utc
REPORT_TIME = datetime(2026, 8, 29, 2, 5, tzinfo=UTC)
SOURCE_TIME = datetime(2026, 8, 29, 2, 5, 11, tzinfo=UTC)
ARTIFACT_ID = "0000000000000042"


def collection() -> DispatchScadaCollection:
    filename = f"PUBLIC_DISPATCHSCADA_202608291205_{ARTIFACT_ID}.zip"
    reference = DispatchScadaArtifactRef(
        url="https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
        zip_filename=filename,
        source_artifact_id=ARTIFACT_ID,
        report_timestamp=REPORT_TIME,
    )
    return DispatchScadaCollection(
        artifact=DispatchScadaArtifact(
            reference=reference,
            csv_member_name=filename.removesuffix(".zip") + ".CSV",
            csv_payload="validated-csv",
            zip_sha256="a" * 64,
            raw_zip=b"validated-zip",
        ),
        records=(
            GeneratorPower5m("BAT1", REPORT_TIME, -4.5, ARTIFACT_ID, SOURCE_TIME, 0),
            GeneratorPower5m("UNMAPPED", REPORT_TIME, 8.0, ARTIFACT_ID, SOURCE_TIME, 0),
        ),
    )


def price_collection() -> DispatchPriceCollection:
    filename = f"PUBLIC_DISPATCHIS_202608291205_{ARTIFACT_ID}.zip"
    reference = DispatchPriceArtifactRef(
        url="https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/" + filename,
        zip_filename=filename,
        source_artifact_id=ARTIFACT_ID,
        report_timestamp=REPORT_TIME,
    )
    records = tuple(
        RegionalPrice5m(
            region=region,
            interval_start=REPORT_TIME,
            price_aud_per_mwh=float(index + 40),
            price_status="available",
            source_id=ARTIFACT_ID,
            source_timestamp=SOURCE_TIME,
            ingestion_version=42,
            quality_flags=("aemo_price_status=FIRM",),
        )
        for index, region in enumerate(("NSW1", "QLD1", "SA1", "TAS1", "VIC1"))
    )
    return DispatchPriceCollection(
        artifact=DispatchPriceArtifact(
            reference=reference,
            csv_member_name=filename.removesuffix(".zip") + ".CSV",
            csv_payload="validated-dispatchis-csv",
            zip_sha256="b" * 64,
            raw_zip=b"validated-dispatchis-zip",
        ),
        records=records,
    )


class CapturingIngestor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.call: tuple[Any, ...] | None = None

    def ingest(self, receipt, observations, generators=(), power_records=()):
        self.call = (receipt, tuple(observations), tuple(generators), tuple(power_records))
        return DispatchScadaIngestionResult(len(self.call[1]), len(self.call[3]), False)


class CapturingPriceIngestor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.call: tuple[Any, ...] | None = None

    def ingest(self, receipt, records):
        self.call = (receipt, tuple(records))
        return DispatchPriceIngestionResult(len(self.call[1]), False)


class ClosingConnection:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class CollectorServiceTests(unittest.TestCase):
    def test_cycle_persists_all_raw_rows_and_only_reviewed_battery_power(self) -> None:
        collected = collection()
        asset = BatteryAsset(
            duid="BAT1",
            site_name="Battery One",
            region="NSW1",
            capacity_mw=10.0,
            storage_capacity_mwh=20.0,
            source_id="reviewed-registry",
            source_timestamp=datetime(2025, 3, 31, tzinfo=UTC),
        )
        captured: list[CapturingIngestor] = []

        def collect(*, ingestion_version, correction_version=0):
            self.assertEqual((ingestion_version, correction_version), (0, 0))
            return collected

        def ingestor_factory(connection):
            ingestor = CapturingIngestor(connection)
            captured.append(ingestor)
            return ingestor

        result = run_collection_cycle(
            object(),
            (asset,),
            collect=collect,
            ingestor_factory=ingestor_factory,
        )

        self.assertEqual(result, DispatchScadaIngestionResult(2, 1, False))
        self.assertEqual(len(captured), 1)
        assert captured[0].call is not None
        receipt, raw_rows, generators, power_rows = captured[0].call
        self.assertEqual(receipt.source_artifact_id, ARTIFACT_ID)
        self.assertEqual(receipt.raw_zip, b"validated-zip")
        self.assertEqual(tuple(row.duid for row in raw_rows), ("BAT1", "UNMAPPED"))
        self.assertEqual(tuple(row.ingestion_version for row in raw_rows), (42, 42))
        self.assertEqual(tuple(row.generator_id for row in power_rows), ("BAT1",))
        self.assertEqual(power_rows[0].ingestion_version, 42)
        self.assertEqual(tuple(row.generator_id for row in generators), ("BAT1",))
        self.assertEqual(generators[0].source_id, "reviewed-registry")
        self.assertEqual(generators[0].ingestion_version, 1)

    def test_cycle_preserves_explicit_historical_source_url(self) -> None:
        captured: list[CapturingIngestor] = []

        def ingestor_factory(connection):
            ingestor = CapturingIngestor(connection)
            captured.append(ingestor)
            return ingestor

        run_collection_cycle(
            object(),
            (
                BatteryAsset(
                    "BAT1",
                    "Battery One",
                    "NSW1",
                    10,
                    20,
                    "reviewed-registry",
                    datetime(2025, 3, 31, tzinfo=UTC),
                ),
            ),
            collect=lambda **kwargs: collection(),
            ingestor_factory=ingestor_factory,
            receipt_source_url=(
                "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
                "PUBLIC_DISPATCHSCADA_20260830.zip"
            ),
        )

        assert captured[0].call is not None
        receipt = captured[0].call[0]
        self.assertEqual(
            receipt.source_url,
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
            "PUBLIC_DISPATCHSCADA_20260830.zip",
        )

    def test_price_cycle_persists_raw_artifact_and_five_regions(self) -> None:
        captured: list[CapturingPriceIngestor] = []

        def ingestor_factory(connection):
            ingestor = CapturingPriceIngestor(connection)
            captured.append(ingestor)
            return ingestor

        result = run_price_collection_cycle(
            object(),
            collect=lambda **kwargs: price_collection(),
            ingestor_factory=ingestor_factory,
        )

        self.assertEqual(result, DispatchPriceIngestionResult(5, False))
        assert captured[0].call is not None
        receipt, records = captured[0].call
        self.assertEqual(receipt.raw_zip, b"validated-dispatchis-zip")
        self.assertEqual(receipt.source_artifact_id, ARTIFACT_ID)
        self.assertEqual(
            tuple(record.region for record in records),
            ("NSW1", "QLD1", "SA1", "TAS1", "VIC1"),
        )

    def test_price_cycle_preserves_explicit_historical_source_url(self) -> None:
        captured: list[CapturingPriceIngestor] = []

        def ingestor_factory(connection):
            ingestor = CapturingPriceIngestor(connection)
            captured.append(ingestor)
            return ingestor

        run_price_collection_cycle(
            object(),
            collect=lambda **kwargs: price_collection(),
            ingestor_factory=ingestor_factory,
            receipt_source_url=(
                "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
                "PUBLIC_DISPATCHIS_20260829.zip#PUBLIC_DISPATCHIS_202608291405_123.zip"
            ),
        )

        assert captured[0].call is not None
        receipt, _ = captured[0].call
        self.assertEqual(
            receipt.source_url,
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260829.zip#PUBLIC_DISPATCHIS_202608291405_123.zip",
        )

    def test_database_cycle_closes_connection_when_ingestion_fails(self) -> None:
        connection = ClosingConnection()
        failure = RuntimeError("database write failed")
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )

        def connect(database_url, *, connect_timeout):
            self.assertEqual((database_url, connect_timeout), ("postgresql://private", 10))
            return connection

        class FailingIngestor:
            def __init__(self, unused_connection) -> None:
                pass

            def ingest(self, *args, **kwargs):
                raise failure

        with self.assertRaises(RuntimeError) as raised:
            run_database_cycle(
                "postgresql://private",
                (asset,),
                connect=connect,
                collect=lambda **kwargs: collection(),
                collect_prices=lambda **kwargs: self.fail("price collection must not run"),
                ingestor_factory=FailingIngestor,
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(connection.close_calls, 1)

    def test_current_cycle_reconciles_all_references_and_fetches_claims(self) -> None:
        from batterywatch_api.collector_ledger import CurrentArtifactClaim
        from batterywatch_api.collector_service import run_current_collection_cycle

        references = tuple(
            DispatchScadaArtifactRef(
                url=(
                    "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
                    f"PUBLIC_DISPATCHSCADA_2026082912{minute:02d}_{source_id}.zip"
                ),
                zip_filename=(
                    f"PUBLIC_DISPATCHSCADA_2026082912{minute:02d}_{source_id}.zip"
                ),
                source_artifact_id=source_id,
                report_timestamp=datetime(2026, 8, 29, 2, minute, tzinfo=UTC),
            )
            for minute, source_id in (
                (0, "0000000000000001"),
                (5, "0000000000000002"),
                (10, "0000000000000003"),
            )
        )
        older, middle, newer = references
        reconciled: list[tuple[CurrentArtifactReference, ...]] = []
        fetched: list[str] = []

        class FakeLedger:
            def reconcile(self, refs):
                materialized = tuple(refs)
                if materialized:
                    reconciled.append(materialized)

            def claim_pending(self, feed, *, limit):
                if feed == "dispatch_scada":
                    self.limit = limit
                    return (
                        CurrentArtifactClaim(
                            next(item for item in reconciled[0] if item.source_artifact_id == middle.source_artifact_id),
                            1,
                        ),
                        CurrentArtifactClaim(
                            next(item for item in reconciled[0] if item.source_artifact_id == newer.source_artifact_id),
                            1,
                        ),
                    )
                return ()

            def complete(self, claim):
                del claim

        def collect(reference, *, ingestion_version, correction_version, fetch):
            del ingestion_version, correction_version, fetch
            fetched.append(reference.source_artifact_id)
            return DispatchScadaCollection(
                artifact=DispatchScadaArtifact(
                    reference=reference,
                    csv_member_name=reference.zip_filename.removesuffix(".zip") + ".CSV",
                    csv_payload="validated-csv",
                    zip_sha256="a" * 64,
                    raw_zip=reference.source_artifact_id.encode("ascii"),
                ),
                records=(
                    GeneratorPower5m(
                        generator_id="BAT1",
                        interval_start=reference.report_timestamp,
                        power_mw=0.0,
                        source_id=reference.source_artifact_id,
                        source_timestamp=reference.report_timestamp,
                        ingestion_version=0,
                    ),
                ),
            )

        result = run_current_collection_cycle(
            object(),
            (
                BatteryAsset(
                    "BAT1", "Battery One", "NSW1", 10, 20,
                    "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
                ),
            ),
            discover_scada=lambda **kwargs: references,
            discover_prices=lambda **kwargs: (),
            ledger_factory=lambda connection: FakeLedger(),
            collect_scada_artifact=collect,
            max_artifacts_per_feed=2,
            ingestor_factory=CapturingIngestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(
            tuple(item.source_artifact_id for item in reconciled[0]),
            tuple(item.source_artifact_id for item in references),
        )
        self.assertEqual(fetched, [middle.source_artifact_id, newer.source_artifact_id])
        self.assertEqual(result.counters.attempted, 2)
        self.assertEqual(result.counters.completed, 2)
        self.assertNotIn(older.source_artifact_id, fetched)

    def test_current_cycle_isolates_feed_failures_and_reports_separate_counters(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        scada_filename = "PUBLIC_DISPATCHSCADA_202608291200_1.zip"
        scada_reference = DispatchScadaArtifactRef(
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + scada_filename,
            scada_filename,
            "1",
            datetime(2026, 8, 29, 2, 0, tzinfo=UTC),
        )
        price_reference = price_collection().artifact.reference
        rows: dict[tuple[str, str], CurrentArtifactReference] = {}
        missed: list[str] = []

        class Ledger:
            def reconcile(self, references):
                materialized = tuple(references)
                for item in materialized:
                    rows[(item.feed, item.source_artifact_id)] = item
                return CurrentArtifactLedgerReconciliation(len(materialized), len(materialized))

            def claim_pending(self, feed, *, limit):
                del limit
                return tuple(
                    CurrentArtifactClaim(item, 1)
                    for (row_feed, _source_id), item in rows.items()
                    if row_feed == feed
                )

            def complete(self, claim):
                del claim

            def miss(self, claim, *, error_summary):
                missed.append(claim.source_artifact_id + ":" + error_summary)

        def fail_scada(reference, **kwargs):
            del reference, kwargs
            raise RuntimeError("transport https://secret.invalid/?token=private")

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=lambda **kwargs: (scada_reference,),
            discover_prices=lambda **kwargs: (price_reference,),
            collect_scada_artifact=fail_scada,
            collect_price_artifact=lambda reference, **kwargs: price_collection(),
            ledger_factory=lambda connection: Ledger(),
            ingestor_factory=CapturingIngestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(
            result.dispatch_scada,
            CollectorFeedCounters(1, 1, 0, 0, 1),
        )
        self.assertEqual(
            result.dispatch_price,
            CollectorFeedCounters(1, 1, 1, 0, 0),
        )
        self.assertEqual(result.counters, CollectorCycleCounters(2, 2, 1, 0, 1))
        self.assertEqual(result.price_count, 5)
        self.assertEqual(result.status, "error")
        self.assertEqual(len(missed), 1)
        self.assertTrue(result.errors)
        self.assertNotIn("secret.invalid", " ".join(result.errors))
        self.assertNotIn("token=private", " ".join(result.errors))

    def test_current_discovery_failure_does_not_block_other_feed(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        price_reference = price_collection().artifact.reference
        rows: dict[str, CurrentArtifactReference] = {}

        class Ledger:
            def reconcile(self, references):
                materialized = tuple(references)
                for item in materialized:
                    rows[item.feed] = item
                return CurrentArtifactLedgerReconciliation(len(materialized), len(materialized))

            def claim_pending(self, feed, *, limit):
                del limit
                item = rows.get(feed)
                return () if item is None else (CurrentArtifactClaim(item, 1),)

            def complete(self, claim):
                del claim

        def fail_discovery(**kwargs):
            del kwargs
            raise RuntimeError("index unavailable")

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=fail_discovery,
            discover_prices=lambda **kwargs: (price_reference,),
            collect_price_artifact=lambda reference, **kwargs: price_collection(),
            ledger_factory=lambda connection: Ledger(),
            ingestor_factory=CapturingIngestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(result.dispatch_scada, CollectorFeedCounters())
        self.assertEqual(result.dispatch_price, CollectorFeedCounters(1, 1, 1, 0, 0))
        self.assertEqual(result.price_count, 5)
        self.assertEqual(result.errors, ("dispatch_scada:discover:RuntimeError",))

    def test_completion_failure_after_commit_is_not_marked_missed(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        source_reference = collection().artifact.reference
        missed: list[str] = []

        class Ledger:
            def reconcile(self, references):
                return CurrentArtifactLedgerReconciliation(len(tuple(references)), 1)

            def claim_pending(self, feed, *, limit):
                del limit
                if feed != "dispatch_scada":
                    return ()
                current = CurrentArtifactReference(
                    "dispatch_scada",
                    source_reference.source_artifact_id,
                    source_reference.url,
                    source_reference.zip_filename,
                    source_reference.report_timestamp,
                )
                return (CurrentArtifactClaim(current, 1),)

            def complete(self, claim):
                del claim
                raise RuntimeError("completion unavailable")

            def miss(self, claim, *, error_summary):
                missed.append(claim.source_artifact_id + error_summary)

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=lambda **kwargs: (source_reference,),
            discover_prices=lambda **kwargs: (),
            collect_scada_artifact=lambda reference, **kwargs: collection(),
            ledger_factory=lambda connection: Ledger(),
            ingestor_factory=CapturingIngestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(result.raw_observation_count, 2)
        self.assertEqual(result.dispatch_scada, CollectorFeedCounters(1, 1, 0, 0, 0))
        self.assertEqual(missed, [])
        self.assertEqual(result.errors, ("dispatch_scada:complete:RuntimeError",))

    def test_database_cycle_defaults_to_current_ledger_runtime(self) -> None:
        connection = ClosingConnection()
        expected = CollectorCycleResult(
            DispatchScadaIngestionResult(0, 0, False),
            DispatchPriceIngestionResult(0, False),
        )
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )

        with patch(
            "batterywatch_api.collector_service.run_current_collection_cycle",
            return_value=expected,
        ) as current:
            result = run_database_cycle(
                "postgresql://private",
                (asset,),
                connect=lambda database_url, *, connect_timeout: connection,
            )

        self.assertIs(result, expected)
        current.assert_called_once()
        self.assertEqual(connection.close_calls, 1)

    def test_polling_loop_emits_one_result_before_clean_stop(self) -> None:
        result = CollectorCycleResult(
            DispatchScadaIngestionResult(0, 0, False),
            DispatchPriceIngestionResult(0, False),
        )
        emitted: list[CollectorCycleResult] = []
        waits: list[float] = []

        returned = run_polling_loop(
            lambda: result,
            interval_seconds=180,
            wait=lambda seconds: (waits.append(seconds) or True),
            emit=emitted.append,
        )

        self.assertIs(returned, result)
        self.assertEqual(emitted, [result])
        self.assertEqual(waits, [180.0])

    def test_main_long_running_cycle_does_not_duplicate_emitted_result(self) -> None:
        result = CollectorCycleResult(
            DispatchScadaIngestionResult(0, 0, False),
            DispatchPriceIngestionResult(0, False),
            CollectorCycleCounters(2, 1, 1, 0, 0),
            dispatch_scada=CollectorFeedCounters(1, 1, 1, 0, 0),
            dispatch_price=CollectorFeedCounters(1, 0, 0, 0, 0),
        )
        output = StringIO()

        def fake_loop(cycle, *, interval_seconds, wait, emit):
            self.assertEqual(interval_seconds, 180)
            del wait
            emitted = cycle()
            emit(emitted)
            return emitted

        with patch(
            "batterywatch_api.collector_service.run_database_cycle",
            return_value=result,
        ), patch(
            "batterywatch_api.collector_service.run_polling_loop",
            side_effect=fake_loop,
        ):
            with redirect_stdout(output):
                exit_code = main(
                    [],
                    environ={
                        "BATTERYWATCH_DATABASE_URL": "postgresql://private",
                        "BATTERYWATCH_ASSETS_PATH": str(
                            Path(__file__).resolve().parents[2]
                            / "config"
                            / "battery_assets.json"
                        ),
                    },
                )

        self.assertEqual(exit_code, 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["dispatch_scada"], {
            "discovered": 1,
            "attempted": 1,
            "completed": 1,
            "replayed": 0,
            "missed": 0,
        })
        self.assertEqual(payload["dispatch_price"]["discovered"], 1)
        self.assertEqual(payload["discovered"], 2)

    def test_polling_loop_runs_immediately_then_stops_during_wait(self) -> None:
        cycle_calls = []
        waits = []

        def cycle():
            cycle_calls.append("cycle")
            return CollectorCycleResult(
                DispatchScadaIngestionResult(2, 1, False),
                DispatchPriceIngestionResult(5, False),
            )

        def wait(seconds):
            waits.append(seconds)
            return True

        result = run_polling_loop(cycle, interval_seconds=300, wait=wait)

        self.assertEqual(
            result,
            CollectorCycleResult(
                DispatchScadaIngestionResult(2, 1, False),
                DispatchPriceIngestionResult(5, False),
            ),
        )
        self.assertEqual((cycle_calls, waits), (["cycle"], [300]))

    def test_once_entrypoint_uses_environment_and_closes_connection(self) -> None:
        connection = ClosingConnection()
        captured: list[CapturingIngestor] = []

        def connect(database_url, *, connect_timeout):
            self.assertEqual((database_url, connect_timeout), ("postgresql://private", 10))
            return connection

        def ingestor_factory(active_connection):
            ingestor = CapturingIngestor(active_connection)
            captured.append(ingestor)
            return ingestor

        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(
                ["--once"],
                environ={
                    "BATTERYWATCH_DATABASE_URL": "postgresql://private",
                    "BATTERYWATCH_ASSETS_PATH": str(
                        Path(__file__).resolve().parents[2]
                        / "config"
                        / "battery_assets.json"
                    ),
                },
                connect=connect,
                collect=lambda **kwargs: collection(),
                collect_prices=lambda **kwargs: price_collection(),
                ingestor_factory=ingestor_factory,
                price_ingestor_factory=CapturingPriceIngestor,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(connection.close_calls, 1)
        self.assertEqual(len(captured), 1)
        self.assertIn('"status": "ok"', output.getvalue())
        self.assertIn('"price_count": 5', output.getvalue())

    def test_cli_safety_arguments_override_environment_file_values(self) -> None:
        connection = ClosingConnection()

        def connect(database_url, *, connect_timeout):
            self.assertEqual((database_url, connect_timeout), ("postgresql://private", 10))
            return connection

        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "--once",
                    "--assets-path",
                    str(
                        Path(__file__).resolve().parents[2]
                        / "config"
                        / "battery_assets.json"
                    ),
                    "--interval-seconds",
                    "300",
                ],
                environ={
                    "BATTERYWATCH_DATABASE_URL": "postgresql://private",
                    "BATTERYWATCH_ASSETS_PATH": "/unreviewed/assets.json",
                    "BATTERYWATCH_COLLECT_INTERVAL_SECONDS": "invalid",
                },
                connect=connect,
                collect=lambda **kwargs: collection(),
                collect_prices=lambda **kwargs: price_collection(),
                ingestor_factory=CapturingIngestor,
                price_ingestor_factory=CapturingPriceIngestor,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(connection.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
