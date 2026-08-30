"""Tests for one claimed historical Dispatch SCADA archive item."""

from datetime import date, datetime, timezone
from typing import Any
import unittest

from batterywatch_api.backfill_artifacts import BackfillArtifactResult
from batterywatch_api.backfill_ledger import (
    BackfillClaim,
    BackfillItemCompletion,
)
from batterywatch_api.battery_assets import BatteryAsset
from batterywatch_api.collector import DispatchScadaCollection
from batterywatch_api.dispatch_scada_ingestion import DispatchScadaIngestionResult
from batterywatch_api.historical_scada_backfill import (
    HistoricalScadaBackfillResult,
    run_scada_backfill_claim,
)
from batterywatch_api.nemweb_archives import (
    DISPATCH_SCADA_FEED,
    NemwebArchiveExtraction,
    NemwebNestedArchiveArtifact,
    NemwebOuterArchiveArtifact,
)
from batterywatch_api.nemweb_dispatch_scada import DispatchScadaArtifact
from batterywatch_api.nemweb_http import NemwebHttpResource
from batterywatch_api.storage import GeneratorPower5m


UTC = timezone.utc
REPORT_DATE = date(2026, 8, 29)
START = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
END = datetime(2026, 8, 29, 14, 0, tzinfo=UTC)
OUTER_URL = (
    "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
    "PUBLIC_DISPATCHSCADA_20260829.zip"
)
CLAIM = BackfillClaim("run-1", DISPATCH_SCADA_FEED, REPORT_DATE, OUTER_URL, 1)
ASSET = BatteryAsset(
    "BAT1",
    "Battery One",
    "NSW1",
    10,
    20,
    "reviewed-registry",
    datetime(2025, 3, 31, tzinfo=UTC),
)


class FakeConnection:
    def __init__(self, sequence: int) -> None:
        self.sequence = sequence
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class HistoricalScadaBackfillTests(unittest.TestCase):
    def test_ingests_two_ordered_intervals_with_fresh_connections(self) -> None:
        connections: list[FakeConnection] = []
        operations: list[tuple[Any, ...]] = []

        def connect(database_url: str, *, connect_timeout: int) -> FakeConnection:
            self.assertEqual((database_url, connect_timeout), ("postgresql://private", 10))
            connection = FakeConnection(len(connections) + 1)
            connections.append(connection)
            return connection

        outer = NemwebOuterArchiveArtifact(
            DISPATCH_SCADA_FEED,
            REPORT_DATE,
            OUTER_URL,
            "PUBLIC_DISPATCHSCADA_20260829.zip",
            "a" * 64,
            b"outer",
        )
        nested = tuple(
            NemwebNestedArchiveArtifact(
                outer,
                f"PUBLIC_DISPATCHSCADA_20260829{hour:02d}05_{source_id}.zip",
                source_id,
                datetime(2026, 8, 28, hour, 5, tzinfo=UTC),
                "b" * 64,
                f"nested-{source_id}".encode(),
            )
            for hour, source_id in ((14, "101"), (15, "102"))
        )

        def fetch(url: str, *, max_bytes: int) -> NemwebHttpResource:
            self.assertEqual(url, OUTER_URL)
            self.assertGreater(max_bytes, 0)
            return NemwebHttpResource(
                url, url, b"outer", "application/zip", None,
                "Sat, 29 Aug 2026 01:02:03 GMT",
            )

        def extract_archive(*args: Any, **kwargs: Any) -> NemwebArchiveExtraction:
            operations.append(("extract", args, kwargs))
            return NemwebArchiveExtraction(outer, nested)

        testcase = self

        class Registrar:
            def __init__(self, connection: FakeConnection) -> None:
                self.connection = connection

            def record(self, receipt: Any) -> BackfillArtifactResult:
                testcase.assertEqual(
                    receipt.downloaded_at,
                    datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC),
                )
                testcase.assertEqual(
                    receipt.source_last_modified,
                    datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC),
                )
                testcase.assertEqual(receipt.raw_archive, b"outer")
                operations.append(("record", self.connection.sequence, receipt))
                return BackfillArtifactResult("a" * 64, 5, True)

        class Ledger:
            def __init__(self, connection: FakeConnection) -> None:
                self.connection = connection

            def complete(self, claim: BackfillClaim, *, records_imported: int) -> BackfillItemCompletion:
                operations.append(
                    ("complete", self.connection.sequence, claim, records_imported)
                )
                return BackfillItemCompletion(True, records_imported)

            def fail(self, claim: BackfillClaim, *, error_summary: str) -> Any:
                raise AssertionError("failure path must not run")

        def extract_zip(reference: Any, payload: bytes) -> DispatchScadaArtifact:
            return DispatchScadaArtifact(
                reference,
                reference.zip_filename.removesuffix(".zip") + ".CSV",
                "validated-csv",
                "b" * 64,
                payload,
            )

        def parse_csv(payload: str, **kwargs: Any) -> tuple[GeneratorPower5m, ...]:
            source_id = kwargs["source_artifact_id"]
            timestamp = next(
                item.interval_timestamp for item in nested
                if item.source_artifact_id == source_id
            )
            return (
                GeneratorPower5m(
                    "BAT1", timestamp, 1.5, source_id, timestamp, 0, 0
                ),
                GeneratorPower5m(
                    "UNMAPPED", timestamp, -2.0, source_id, timestamp, 0, 0
                ),
            )

        def ingest_cycle(
            connection: FakeConnection,
            assets: tuple[BatteryAsset, ...],
            *,
            collect: Any,
            receipt_source_url: str,
        ) -> DispatchScadaIngestionResult:
            collection = collect(ingestion_version=0, correction_version=0)
            operations.append(
                (
                    "ingest",
                    connection.sequence,
                    collection.artifact.reference.source_artifact_id,
                    receipt_source_url,
                    tuple(asset.duid for asset in assets),
                )
            )
            return DispatchScadaIngestionResult(2, 1, True)

        result = run_scada_backfill_claim(
            "postgresql://private",
            (ASSET,),
            CLAIM,
            START,
            END,
            connect=connect,
            fetch=fetch,
            clock=lambda: datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC),
            registrar_factory=Registrar,
            ledger_factory=Ledger,
            extract_archive=extract_archive,
            extract_zip=extract_zip,
            parse_csv=parse_csv,
            ingest_cycle=ingest_cycle,
        )

        self.assertEqual(
            result,
            HistoricalScadaBackfillResult(2, 4, 2, 2, True, True),
        )
        self.assertEqual([connection.close_calls for connection in connections], [1, 1, 1, 1])
        self.assertEqual(
            [operation[0] for operation in operations],
            ["record", "extract", "ingest", "ingest", "complete"],
        )
        self.assertEqual(
            [operation[3] for operation in operations if operation[0] == "ingest"],
            [f"{OUTER_URL}#{nested[0].member_name}", f"{OUTER_URL}#{nested[1].member_name}"],
        )
        self.assertEqual(operations[-1][-1], 4)

    def test_malformed_last_modified_records_sanitized_failure(self) -> None:
        connections: list[FakeConnection] = []
        failures: list[tuple[BackfillClaim, str]] = []

        def connect(database_url: str, *, connect_timeout: int) -> FakeConnection:
            connection = FakeConnection(len(connections) + 1)
            connections.append(connection)
            return connection

        class Ledger:
            def __init__(self, connection: FakeConnection) -> None:
                self.connection = connection

            def fail(self, claim: BackfillClaim, *, error_summary: str) -> Any:
                failures.append((claim, error_summary))
                return object()

        with self.assertRaisesRegex(ValueError, "Last-Modified") as raised:
            run_scada_backfill_claim(
                "postgresql://private",
                (ASSET,),
                CLAIM,
                START,
                END,
                connect=connect,
                fetch=lambda *args, **kwargs: NemwebHttpResource(
                    OUTER_URL,
                    OUTER_URL,
                    b"outer",
                    "application/zip",
                    None,
                    "not-an-http-date",
                ),
                clock=lambda: datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC),
                ledger_factory=Ledger,
            )

        self.assertIsInstance(raised.exception, ValueError)
        self.assertEqual(failures, [(CLAIM, "ValueError")])
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0].close_calls, 1)

    def test_nested_failure_after_success_records_failure_and_reraises_original(self) -> None:
        connections: list[FakeConnection] = []
        failures: list[tuple[BackfillClaim, str, int]] = []
        failure = RuntimeError("secret-bearing database detail")
        ingest_calls = 0

        def connect(database_url: str, *, connect_timeout: int) -> FakeConnection:
            connection = FakeConnection(len(connections) + 1)
            connections.append(connection)
            return connection

        outer = NemwebOuterArchiveArtifact(
            DISPATCH_SCADA_FEED,
            REPORT_DATE,
            OUTER_URL,
            "PUBLIC_DISPATCHSCADA_20260829.zip",
            "a" * 64,
            b"outer",
        )
        nested = tuple(
            NemwebNestedArchiveArtifact(
                outer,
                f"PUBLIC_DISPATCHSCADA_202608291{index}05_{100 + index}.zip",
                str(100 + index),
                datetime(2026, 8, 28, 14 + index, 5, tzinfo=UTC),
                "b" * 64,
                f"nested-{index}".encode(),
            )
            for index in (0, 1)
        )

        class Registrar:
            def __init__(self, connection: FakeConnection) -> None:
                self.connection = connection

            def record(self, receipt: Any) -> BackfillArtifactResult:
                return BackfillArtifactResult("a" * 64, 5, False)

        class Ledger:
            def __init__(self, connection: FakeConnection) -> None:
                self.connection = connection

            def fail(self, claim: BackfillClaim, *, error_summary: str) -> Any:
                failures.append((claim, error_summary, self.connection.sequence))
                return object()

            def complete(self, claim: BackfillClaim, *, records_imported: int) -> Any:
                raise AssertionError("completion must not run")

        def extract_zip(reference: Any, payload: bytes) -> DispatchScadaArtifact:
            return DispatchScadaArtifact(reference, "member.CSV", "csv", "b" * 64, payload)

        def parse_csv(payload: str, **kwargs: Any) -> tuple[GeneratorPower5m, ...]:
            source_id = kwargs["source_artifact_id"]
            timestamp = next(
                item.interval_timestamp for item in nested
                if item.source_artifact_id == source_id
            )
            return (GeneratorPower5m("BAT1", timestamp, 1, source_id, timestamp, 0, 0),)

        def ingest_cycle(*args: Any, **kwargs: Any) -> DispatchScadaIngestionResult:
            nonlocal ingest_calls
            ingest_calls += 1
            if ingest_calls == 2:
                raise failure
            return DispatchScadaIngestionResult(1, 1, False)

        with self.assertRaises(RuntimeError) as raised:
            run_scada_backfill_claim(
                "postgresql://private",
                (ASSET,),
                CLAIM,
                START,
                END,
                connect=connect,
                fetch=lambda *args, **kwargs: NemwebHttpResource(
                    OUTER_URL, OUTER_URL, b"outer", "application/zip", None, None
                ),
                clock=lambda: datetime(2026, 8, 30, tzinfo=UTC),
                registrar_factory=Registrar,
                ledger_factory=Ledger,
                extract_archive=lambda *args, **kwargs: NemwebArchiveExtraction(outer, nested),
                extract_zip=extract_zip,
                parse_csv=parse_csv,
                ingest_cycle=ingest_cycle,
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(ingest_calls, 2)
        self.assertEqual(failures, [(CLAIM, "RuntimeError", 4)])
        self.assertEqual([connection.close_calls for connection in connections], [1, 1, 1, 1])

    def test_failure_recording_error_does_not_replace_original(self) -> None:
        original = RuntimeError("original secret-bearing failure")

        def fetch(*args: Any, **kwargs: Any) -> Any:
            raise original

        def connect(*args: Any, **kwargs: Any) -> Any:
            raise OSError("secondary failure")

        with self.assertRaises(RuntimeError) as raised:
            run_scada_backfill_claim(
                "postgresql://private",
                (ASSET,),
                CLAIM,
                START,
                END,
                connect=connect,
                fetch=fetch,
            )

        self.assertIs(raised.exception, original)

    def test_invalid_inputs_fail_before_fetch_or_connect(self) -> None:
        side_effects: list[str] = []

        def fetch(*args: Any, **kwargs: Any) -> Any:
            side_effects.append("fetch")
            raise AssertionError("fetch must not run")

        def connect(*args: Any, **kwargs: Any) -> Any:
            side_effects.append("connect")
            raise AssertionError("connect must not run")

        invalid_claim = BackfillClaim("run-1", "dispatch_price", REPORT_DATE, OUTER_URL, 1)
        cases = (
            ("", CLAIM, START, END),
            ("postgresql://private", invalid_claim, START, END),
            ("postgresql://private", CLAIM, START.replace(tzinfo=None), END),
            ("postgresql://private", CLAIM, END, START),
        )
        for database_url, claim, start, end in cases:
            with self.subTest(database_url=database_url, claim=claim, start=start, end=end):
                with self.assertRaises(ValueError):
                    run_scada_backfill_claim(
                        database_url,
                        (ASSET,),
                        claim,
                        start,
                        end,
                        connect=connect,
                        fetch=fetch,
                    )

        self.assertEqual(side_effects, [])

    def test_zero_selected_intervals_fails_closed_without_completion(self) -> None:
        connections: list[FakeConnection] = []
        failures: list[str] = []
        outer = NemwebOuterArchiveArtifact(
            DISPATCH_SCADA_FEED,
            REPORT_DATE,
            OUTER_URL,
            "PUBLIC_DISPATCHSCADA_20260829.zip",
            "a" * 64,
            b"outer",
        )

        def connect(*args: Any, **kwargs: Any) -> FakeConnection:
            connection = FakeConnection(len(connections) + 1)
            connections.append(connection)
            return connection

        class Registrar:
            def __init__(self, connection: FakeConnection) -> None:
                self.connection = connection

            def record(self, receipt: Any) -> BackfillArtifactResult:
                return BackfillArtifactResult("a" * 64, 5, False)

        class Ledger:
            def __init__(self, connection: FakeConnection) -> None:
                self.connection = connection

            def fail(self, claim: BackfillClaim, *, error_summary: str) -> Any:
                failures.append(error_summary)
                return object()

            def complete(self, claim: BackfillClaim, *, records_imported: int) -> Any:
                raise AssertionError("completion must not run")

        with self.assertRaisesRegex(ValueError, "no selected"):
            run_scada_backfill_claim(
                "postgresql://private",
                (ASSET,),
                CLAIM,
                START,
                END,
                connect=connect,
                fetch=lambda *args, **kwargs: NemwebHttpResource(
                    OUTER_URL, OUTER_URL, b"outer", "application/zip", None, None
                ),
                clock=lambda: datetime(2026, 8, 30, tzinfo=UTC),
                registrar_factory=Registrar,
                ledger_factory=Ledger,
                extract_archive=lambda *args, **kwargs: NemwebArchiveExtraction(outer, ()),
            )

        self.assertEqual(failures, ["ValueError"])
        self.assertEqual([connection.close_calls for connection in connections], [1, 1])


if __name__ == "__main__":
    unittest.main()
