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

    def test_polling_loop_stops_after_a_non_successful_cycle(self) -> None:
        result = CollectorCycleResult(
            DispatchScadaIngestionResult(0, 0, False),
            DispatchPriceIngestionResult(0, False),
            errors=("dispatch_scada:claim:RuntimeError",),
        )

        def wait(seconds):
            del seconds
            raise AssertionError("a failed cycle must not be hidden by the wait")

        self.assertIs(
            run_polling_loop(
                lambda: result,
                interval_seconds=300,
                wait=wait,
            ),
            result,
        )

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

    def test_current_cycle_reconciles_and_processes_old_and_new_scada_files(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        base = collection()
        old_filename = "PUBLIC_DISPATCHSCADA_202608291200_0000000000000001.zip"
        new_filename = "PUBLIC_DISPATCHSCADA_202608291205_0000000000000002.zip"
        old_ref = DispatchScadaArtifactRef(
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + old_filename,
            old_filename,
            "0000000000000001",
            datetime(2026, 8, 29, 2, 0, tzinfo=UTC),
        )
        new_ref = DispatchScadaArtifactRef(
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + new_filename,
            new_filename,
            "0000000000000002",
            datetime(2026, 8, 29, 2, 5, tzinfo=UTC),
        )
        ledger_references: tuple[CurrentArtifactReference, ...] = ()
        fetched: list[str] = []
        completed: list[str] = []

        class Ledger:
            def reconcile(self, references):
                nonlocal ledger_references
                materialized = tuple(references)
                if materialized:
                    ledger_references = materialized
                return CurrentArtifactLedgerReconciliation(len(materialized), len(materialized))

            def claim_pending(self, feed, *, limit):
                self.assert_feed = feed
                self.assert_limit = limit
                return tuple(
                    CurrentArtifactClaim(reference, 1)
                    for reference in ledger_references
                    if reference.feed == feed
                )

            def complete(self, claim):
                completed.append(claim.source_artifact_id)

            def miss(self, claim, *, error_summary):
                raise AssertionError(error_summary)

        def collect_one(reference, ingestion_version, correction_version, *, fetch):
            del ingestion_version, correction_version, fetch
            fetched.append(reference.source_artifact_id)
            return replace(
                base,
                artifact=replace(base.artifact, reference=reference),
                records=(),
            )

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=lambda **kwargs: (old_ref, new_ref),
            discover_prices=lambda **kwargs: (),
            collect_scada_artifact=collect_one,
            ledger_factory=lambda connection: Ledger(),
            ingestor_factory=CapturingIngestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(tuple(item.source_artifact_id for item in ledger_references), (
            "0000000000000001", "0000000000000002",
        ))
        self.assertEqual(fetched, [
            "0000000000000001", "0000000000000002",
        ])
        self.assertEqual(completed, fetched)
        self.assertEqual(
            (result.counters.discovered, result.counters.attempted,
             result.counters.completed, result.counters.missed),
            (2, 2, 2, 0),
        )

    def test_failed_current_file_is_missed_then_retried_without_rows(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        filename = "PUBLIC_DISPATCHSCADA_202608291200_0000000000000001.zip"
        source_reference = DispatchScadaArtifactRef(
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
            filename,
            "0000000000000001",
            datetime(2026, 8, 29, 2, 0, tzinfo=UTC),
        )
        current_reference = CurrentArtifactReference(
            "dispatch_scada",
            source_reference.source_artifact_id,
            source_reference.url,
            source_reference.zip_filename,
            source_reference.report_timestamp,
        )
        base = collection()

        class Ledger:
            def __init__(self):
                self.status = {}
                self.attempts = {}
                self.errors = []

            def reconcile(self, references):
                for reference in references:
                    self.status.setdefault(reference.source_artifact_id, "pending")
                    self.attempts.setdefault(reference.source_artifact_id, 0)
                return CurrentArtifactLedgerReconciliation(len(tuple(references)), 0)

            def claim_pending(self, feed, *, limit):
                del limit
                return tuple(
                    CurrentArtifactClaim(
                        current_reference,
                        self.attempts[current_reference.source_artifact_id] + 1,
                    )
                    for source_id, status in self.status.items()
                    if status in {"pending", "missed"}
                    and current_reference.feed == feed
                    and source_id == current_reference.source_artifact_id
                    for _ in (
                        self.attempts.__setitem__(
                            source_id, self.attempts[source_id] + 1
                        ),
                    )
                )

            def complete(self, claim):
                self.status[claim.source_artifact_id] = "completed"

            def miss(self, claim, *, error_summary):
                self.status[claim.source_artifact_id] = "missed"
                self.errors.append(error_summary)

        ledger = Ledger()
        calls = 0

        def collect_one(reference, ingestion_version, correction_version, *, fetch):
            nonlocal calls
            del ingestion_version, correction_version, fetch
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary transport failure")
            return replace(
                base,
                artifact=replace(base.artifact, reference=reference),
                records=(),
            )

        def run_once():
            return run_current_collection_cycle(
                object(),
                (asset,),
                discover_scada=lambda **kwargs: (source_reference,),
                discover_prices=lambda **kwargs: (),
                collect_scada_artifact=collect_one,
                ledger_factory=lambda connection: ledger,
                ingestor_factory=CapturingIngestor,
                price_ingestor_factory=CapturingPriceIngestor,
            )

        first = run_once()
        second = run_once()

        self.assertEqual(first.counters, CollectorCycleCounters(1, 1, 0, 0, 1))
        self.assertEqual(
            (second.counters.attempted, second.counters.completed,
             second.counters.missed, ledger.status[current_reference.source_artifact_id]),
            (1, 1, 0, "completed"),
        )
        self.assertEqual(len(ledger.errors), 1)

    def test_current_cycle_counts_only_the_artifacts_that_replayed(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        references = tuple(
            DispatchScadaArtifactRef(
                "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
                + filename,
                filename,
                source_id,
                report_time,
            )
            for filename, source_id, report_time in (
                (
                    "PUBLIC_DISPATCHSCADA_202608291200_0000000000000001.zip",
                    "0000000000000001",
                    datetime(2026, 8, 29, 2, 0, tzinfo=UTC),
                ),
                (
                    "PUBLIC_DISPATCHSCADA_202608291205_0000000000000002.zip",
                    "0000000000000002",
                    datetime(2026, 8, 29, 2, 5, tzinfo=UTC),
                ),
            )
        )
        ledger_refs: tuple[CurrentArtifactReference, ...] = ()

        class Ledger:
            def reconcile(self, refs):
                nonlocal ledger_refs
                ledger_refs = tuple(refs)
                return CurrentArtifactLedgerReconciliation(len(ledger_refs), len(ledger_refs))

            def claim_pending(self, feed, *, limit):
                del limit
                return tuple(
                    CurrentArtifactClaim(ref, 1)
                    for ref in ledger_refs
                    if ref.feed == feed
                )

            def complete(self, claim):
                del claim

            def miss(self, claim, *, error_summary):
                raise AssertionError((claim, error_summary))

        base = collection()
        ingest_count = 0

        def collect_one(reference, ingestion_version, correction_version, *, fetch):
            del ingestion_version, correction_version, fetch
            return replace(
                base,
                artifact=replace(base.artifact, reference=reference),
                records=(),
            )

        class Ingestor:
            def __init__(self, connection):
                del connection

            def ingest(self, receipt, observations, generators=(), power_records=()):
                nonlocal ingest_count
                del receipt, observations, generators, power_records
                ingest_count += 1
                return DispatchScadaIngestionResult(0, 0, ingest_count == 1)

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=lambda **kwargs: references,
            discover_prices=lambda **kwargs: (),
            collect_scada_artifact=collect_one,
            ledger_factory=lambda connection: Ledger(),
            ingestor_factory=Ingestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(result.counters, CollectorCycleCounters(2, 2, 2, 1, 0))

    def test_database_cycle_uses_current_reconciliation_when_ledger_is_supplied(self) -> None:
        connection = ClosingConnection()
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        filename = "PUBLIC_DISPATCHSCADA_202608291200_0000000000000001.zip"
        reference = DispatchScadaArtifactRef(
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
            filename,
            "0000000000000001",
            datetime(2026, 8, 29, 2, 0, tzinfo=UTC),
        )
        seen = []

        class Ledger:
            def reconcile(self, references):
                seen.extend(references)
                return CurrentArtifactLedgerReconciliation(len(tuple(references)), 0)

            def claim_pending(self, feed, *, limit):
                del feed, limit
                return ()

        def connect(database_url, *, connect_timeout):
            self.assertEqual((database_url, connect_timeout), ("postgresql://private", 10))
            return connection

        result = run_database_cycle(
            "postgresql://private",
            (asset,),
            connect=connect,
            ledger_factory=lambda active_connection: Ledger(),
            discover_scada=lambda **kwargs: (reference,),
            discover_prices=lambda **kwargs: (),
        )

        self.assertEqual(result.counters.discovered, 1)
        self.assertEqual(tuple(item.source_artifact_id for item in seen), (
            "0000000000000001",
        ))
        self.assertEqual(connection.close_calls, 1)

    def test_deployed_collector_cadence_defaults_to_three_minutes(self) -> None:
        app_root = Path(__file__).resolve().parents[2]
        service_unit = (app_root / "deploy" / "batterywatch-collector.service").read_text(
            encoding="utf-8"
        )
        deploy_docs = (app_root / "deploy" / "README.md").read_text(encoding="utf-8")

        self.assertIn("--interval-seconds 180", service_unit)
        self.assertIn("180 seconds", deploy_docs)

    def test_default_interval_is_180_and_explicit_cli_interval_wins(self) -> None:
        intervals: list[int] = []
        result = CollectorCycleResult(
            DispatchScadaIngestionResult(0, 0, False),
            DispatchPriceIngestionResult(0, False),
        )

        def fake_loop(cycle, *, interval_seconds, wait):
            del cycle, wait
            intervals.append(interval_seconds)
            return result

        environment = {
            "BATTERYWATCH_DATABASE_URL": "postgresql://private",
            "BATTERYWATCH_ASSETS_PATH": str(
                Path(__file__).resolve().parents[2]
                / "config"
                / "battery_assets.json"
            ),
        }
        with patch("batterywatch_api.collector_service.run_polling_loop", fake_loop):
            self.assertEqual(
                main(
                    [],
                    environ=environment,
                    connect=lambda *args, **kwargs: self.fail("cycle was not run"),
                ),
                0,
            )
            self.assertEqual(
                main(
                    ["--interval-seconds", "45"],
                    environ=environment,
                    connect=lambda *args, **kwargs: self.fail("cycle was not run"),
                ),
                0,
            )

        self.assertEqual(intervals, [180, 45])

    def test_scada_failure_does_not_block_price_and_error_is_bounded(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        scada_filename = "PUBLIC_DISPATCHSCADA_202608291200_0000000000000001.zip"
        scada_ref = DispatchScadaArtifactRef(
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + scada_filename,
            scada_filename,
            "0000000000000001",
            datetime(2026, 8, 29, 2, 0, tzinfo=UTC),
        )
        price_filename = f"PUBLIC_DISPATCHIS_202608291205_{ARTIFACT_ID}.zip"
        price_ref = DispatchPriceArtifactRef(
            "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/" + price_filename,
            price_filename,
            ARTIFACT_ID,
            REPORT_TIME,
        )
        rows = {}
        errors: list[str] = []

        class Ledger:
            def reconcile(self, references):
                materialized = tuple(references)
                for reference in materialized:
                    rows.setdefault((reference.feed, reference.source_artifact_id),
                                    [reference, "pending", 0])
                return CurrentArtifactLedgerReconciliation(len(materialized), len(materialized))

            def claim_pending(self, feed, *, limit):
                selected = [
                    row for (row_feed, _source_id), row in rows.items()
                    if row_feed == feed and row[1] in {"pending", "missed"}
                ][:limit]
                claims = []
                for row in selected:
                    row[2] += 1
                    claims.append(CurrentArtifactClaim(row[0], row[2]))
                return tuple(claims)

            def complete(self, claim):
                rows[(claim.feed, claim.source_artifact_id)][1] = "completed"

            def miss(self, claim, *, error_summary):
                rows[(claim.feed, claim.source_artifact_id)][1] = "missed"
                errors.append(error_summary)

        def fail_scada(reference, ingestion_version, correction_version, *, fetch):
            del reference, ingestion_version, correction_version, fetch
            raise RuntimeError("transport https://secret.invalid/?token=private")

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=lambda **kwargs: (scada_ref,),
            discover_prices=lambda **kwargs: (price_ref,),
            collect_scada_artifact=fail_scada,
            collect_price_artifact=lambda reference, **kwargs: price_collection(),
            ledger_factory=lambda connection: Ledger(),
            ingestor_factory=CapturingIngestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(result.counters, CollectorCycleCounters(2, 2, 1, 0, 1))
        self.assertEqual(result.price_count, 5)
        self.assertEqual(result.raw_observation_count, 0)
        self.assertEqual(len(errors), 1)
        self.assertLessEqual(len(errors[0]), 512)
        self.assertNotIn("secret.invalid", errors[0])
        self.assertNotIn("token=private", errors[0])

    def test_current_discovery_failure_is_visible_without_blocking_other_feed(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        price_reference = price_collection().artifact.reference
        references: dict[str, CurrentArtifactReference] = {}

        class Ledger:
            def reconcile(self, current_references):
                materialized = tuple(current_references)
                for reference in materialized:
                    references[reference.feed] = reference
                return CurrentArtifactLedgerReconciliation(
                    len(materialized), len(materialized)
                )

            def claim_pending(self, feed, *, limit):
                del limit
                reference = references.get(feed)
                if reference is None:
                    return ()
                return (CurrentArtifactClaim(reference, 1),)

            def complete(self, claim):
                del claim

            def miss(self, claim, *, error_summary):
                raise AssertionError((claim, error_summary))

        def fail_scada_discovery(**kwargs):
            del kwargs
            raise RuntimeError("SCADA index unavailable")

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=fail_scada_discovery,
            discover_prices=lambda **kwargs: (price_reference,),
            collect_price_artifact=lambda reference, **kwargs: price_collection(),
            ledger_factory=lambda connection: Ledger(),
            ingestor_factory=CapturingIngestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(result.price_count, 5)
        self.assertEqual(result.counters, CollectorCycleCounters(1, 1, 1, 0, 0))
        self.assertEqual(result.status, "error")
        self.assertTrue(result.errors)

    def test_current_claim_failure_is_visible_instead_of_zero_counter_success(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        source_reference = collection().artifact.reference

        class Ledger:
            def reconcile(self, references):
                materialized = tuple(references)
                return CurrentArtifactLedgerReconciliation(
                    len(materialized), len(materialized)
                )

            def claim_pending(self, feed, *, limit):
                del feed, limit
                raise RuntimeError("ledger unavailable")

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=lambda **kwargs: (source_reference,),
            discover_prices=lambda **kwargs: (),
            ledger_factory=lambda connection: Ledger(),
        )

        self.assertEqual(result.counters, CollectorCycleCounters(1, 0, 0, 0, 0))
        self.assertEqual(result.status, "error")
        self.assertTrue(result.errors)

    def test_ingestion_database_failure_is_not_reported_as_success(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        source_reference = collection().artifact.reference
        current_reference = CurrentArtifactReference(
            "dispatch_scada",
            source_reference.source_artifact_id,
            source_reference.url,
            source_reference.zip_filename,
            source_reference.report_timestamp,
        )
        missed: list[str] = []

        class Ledger:
            def reconcile(self, references):
                return CurrentArtifactLedgerReconciliation(len(tuple(references)), 1)

            def claim_pending(self, feed, *, limit):
                del limit
                if feed != "dispatch_scada":
                    return ()
                return (CurrentArtifactClaim(current_reference, 1),)

            def miss(self, claim, *, error_summary):
                del claim
                missed.append(error_summary)

        class FailingIngestor:
            def __init__(self, connection):
                del connection

            def ingest(self, *args, **kwargs):
                del args, kwargs
                raise RuntimeError("database write failed")

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=lambda **kwargs: (source_reference,),
            discover_prices=lambda **kwargs: (),
            collect_scada_artifact=lambda reference, **kwargs: replace(
                collection(), artifact=replace(collection().artifact, reference=reference)
            ),
            ledger_factory=lambda connection: Ledger(),
            ingestor_factory=FailingIngestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(result.counters, CollectorCycleCounters(1, 1, 0, 0, 1))
        self.assertEqual(result.status, "error")
        self.assertTrue(result.errors)
        self.assertEqual(len(missed), 1)

    def test_completion_failure_after_ingest_is_recoverable_not_a_missed_artifact(self) -> None:
        asset = BatteryAsset(
            "BAT1", "Battery One", "NSW1", 10, 20,
            "reviewed-registry", datetime(2025, 3, 31, tzinfo=UTC),
        )
        source_reference = collection().artifact.reference
        current_reference = CurrentArtifactReference(
            "dispatch_scada",
            source_reference.source_artifact_id,
            source_reference.url,
            source_reference.zip_filename,
            source_reference.report_timestamp,
        )
        ingested: list[str] = []
        missed: list[str] = []

        class Ledger:
            status = "pending"

            def reconcile(self, references):
                return CurrentArtifactLedgerReconciliation(len(tuple(references)), 1)

            def claim_pending(self, feed, *, limit):
                del limit
                if feed != "dispatch_scada":
                    return ()
                return (CurrentArtifactClaim(current_reference, 1),)

            def complete(self, claim):
                del claim
                raise RuntimeError("ledger completion unavailable")

            def miss(self, claim, *, error_summary):
                del claim
                missed.append(error_summary)

        class Ingestor:
            def __init__(self, connection):
                del connection

            def ingest(self, *args, **kwargs):
                del args, kwargs
                ingested.append("committed")
                return DispatchScadaIngestionResult(2, 1, False)

        result = run_current_collection_cycle(
            object(),
            (asset,),
            discover_scada=lambda **kwargs: (source_reference,),
            discover_prices=lambda **kwargs: (),
            collect_scada_artifact=lambda reference, **kwargs: replace(
                collection(), artifact=replace(collection().artifact, reference=reference)
            ),
            ledger_factory=lambda connection: Ledger(),
            ingestor_factory=Ingestor,
            price_ingestor_factory=CapturingPriceIngestor,
        )

        self.assertEqual(ingested, ["committed"])
        self.assertEqual(missed, [])
        self.assertEqual(result.counters, CollectorCycleCounters(1, 1, 0, 0, 0))
        self.assertEqual(result.raw_observation_count, 2)
        self.assertEqual(result.status, "error")
        self.assertTrue(result.errors)

    def test_once_json_reports_deterministic_current_work_counters(self) -> None:
        result = CollectorCycleResult(
            DispatchScadaIngestionResult(4, 2, True),
            DispatchPriceIngestionResult(5, False),
            CollectorCycleCounters(7, 5, 4, 1, 1),
        )
        output = StringIO()
        with patch(
            "batterywatch_api.collector_service.run_database_cycle",
            return_value=result,
        ):
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
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            {name: payload[name] for name in (
                "discovered", "attempted", "completed", "replayed", "missed",
            )},
            {"discovered": 7, "attempted": 5, "completed": 4, "replayed": 1, "missed": 1},
        )


if __name__ == "__main__":
    unittest.main()
