"""Tests for the separate Dispatch SCADA collector runtime."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
import unittest

from batterywatch_api.battery_assets import BatteryAsset
from batterywatch_api.collector import DispatchScadaCollection
from batterywatch_api.collector_service import (
    CollectorCycleResult,
    main,
    run_collection_cycle,
    run_database_cycle,
    run_price_collection_cycle,
    run_polling_loop,
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
