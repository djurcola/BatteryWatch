"""Tests for one claimed historical DispatchIS price archive item."""

from datetime import date, datetime, timezone
from typing import Any
import unittest

from batterywatch_api.backfill_artifacts import BackfillArtifactResult
from batterywatch_api.backfill_ledger import BackfillClaim, BackfillItemCompletion
from batterywatch_api.dispatch_price_ingestion import DispatchPriceIngestionResult
from batterywatch_api.nemweb_archives import (
    DISPATCHIS_PRICE_FEED,
    NemwebArchiveExtraction,
    NemwebNestedArchiveArtifact,
    NemwebOuterArchiveArtifact,
)
from batterywatch_api.nemweb_dispatch_prices import (
    DispatchPriceArtifact,
    DispatchPriceArtifactRef,
    DispatchPriceCollection,
)
from batterywatch_api.nemweb_http import NemwebHttpResource
from batterywatch_api.historical_price_backfill import run_price_backfill_claim
from batterywatch_api.storage import RegionalPrice5m

UTC = timezone.utc


class Connection:
    def __init__(self, number: int) -> None:
        self.number = number
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class HistoricalPriceBackfillTests(unittest.TestCase):
    def test_ingests_two_five_region_intervals_with_fresh_connections(self) -> None:
        connections: list[Connection] = []
        operations: list[str] = []
        report_date = date(2026, 8, 29)
        outer_url = (
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260829.zip"
        )
        outer = NemwebOuterArchiveArtifact(
            DISPATCHIS_PRICE_FEED,
            report_date,
            outer_url,
            "PUBLIC_DISPATCHIS_20260829.zip",
            "a" * 64,
            b"outer",
        )
        nested = tuple(
            NemwebNestedArchiveArtifact(
                outer,
                f"PUBLIC_DISPATCHIS_20260829{hour:02d}05_{100 + index}.zip",
                str(100 + index),
                datetime(2026, 8, 29, hour, 5, tzinfo=UTC),
                chr(ord("b") + index) * 64,
                f"nested-{index}".encode(),
            )
            for index, hour in enumerate((14, 15))
        )
        extraction = NemwebArchiveExtraction(outer, nested)
        claim = BackfillClaim(
            "price-run",
            "dispatch_price",
            report_date,
            outer_url,
            1,
        )

        def connect(database_url: str, *, connect_timeout: int) -> Connection:
            self.assertEqual((database_url, connect_timeout), ("postgresql://private", 10))
            connection = Connection(len(connections) + 1)
            connections.append(connection)
            return connection

        def fetch(url: str, *, max_bytes: int) -> NemwebHttpResource:
            self.assertEqual(url, outer_url)
            self.assertEqual(max_bytes, 128 * 1024 * 1024)
            operations.append("fetch")
            return NemwebHttpResource(url, url, b"outer", "application/zip", None, None)

        class Registrar:
            def __init__(self, connection: Connection) -> None:
                self.connection = connection

            def record(self, receipt: Any) -> BackfillArtifactResult:
                operations.append(f"record:{self.connection.number}")
                return BackfillArtifactResult("a" * 64, len(receipt.raw_archive), True)

        def extract_archive(*args: Any, **kwargs: Any) -> NemwebArchiveExtraction:
            self.assertEqual(args[0].feed, DISPATCHIS_PRICE_FEED)
            operations.append("extract")
            return extraction

        def extract_zip(reference: DispatchPriceArtifactRef, payload: bytes) -> DispatchPriceArtifact:
            return DispatchPriceArtifact(
                reference,
                reference.zip_filename.removesuffix(".zip") + ".CSV",
                "csv",
                "d" * 64,
                payload,
            )

        def parse_csv(
            payload: str,
            *,
            source_id: str,
            ingestion_version: int,
            correction_version: int,
        ) -> tuple[RegionalPrice5m, ...]:
            del payload, ingestion_version, correction_version
            timestamp = next(item.interval_timestamp for item in nested if item.source_artifact_id == source_id)
            prices = (100.0, -25.0, 75.0, 0.0, 50.0)
            return tuple(
                RegionalPrice5m(
                    region,
                    timestamp,
                    price,
                    "negative" if price < 0 else "available",
                    source_id,
                    timestamp,
                    int(source_id),
                )
                for region, price in zip(("NSW1", "QLD1", "SA1", "TAS1", "VIC1"), prices)
            )

        def ingest_cycle(
            connection: Connection,
            *,
            collect: Any,
            receipt_source_url: str,
        ) -> DispatchPriceIngestionResult:
            collection: DispatchPriceCollection = collect(ingestion_version=0, correction_version=0)
            operations.append(f"ingest:{connection.number}:{collection.artifact.reference.source_artifact_id}")
            self.assertEqual(receipt_source_url, f"{outer_url}#{collection.artifact.reference.zip_filename}")
            self.assertIn(-25.0, tuple(record.price_aud_per_mwh for record in collection.records))
            return DispatchPriceIngestionResult(0, True)

        class Ledger:
            def __init__(self, connection: Connection) -> None:
                self.connection = connection

            def complete(self, actual_claim: BackfillClaim, *, records_imported: int) -> BackfillItemCompletion:
                self.assert_claim(actual_claim)
                operations.append(f"complete:{self.connection.number}:{records_imported}")
                return BackfillItemCompletion(True, records_imported)

            def assert_claim(self, actual_claim: BackfillClaim) -> None:
                if actual_claim != claim:
                    raise AssertionError("claim mismatch")

            def fail(self, actual_claim: BackfillClaim, *, error_summary: str) -> Any:
                raise AssertionError(f"failure path must not run: {actual_claim} {error_summary}")

        result = run_price_backfill_claim(
            "postgresql://private",
            claim,
            datetime(2026, 8, 29, 14, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 16, 0, tzinfo=UTC),
            connect=connect,
            fetch=fetch,
            clock=lambda: datetime(2026, 8, 30, 3, 0, tzinfo=UTC),
            ledger_factory=Ledger,
            registrar_factory=Registrar,
            extract_archive=extract_archive,
            extract_zip=extract_zip,
            parse_csv=parse_csv,
            ingest_cycle=ingest_cycle,
        )

        self.assertEqual(result.interval_artifact_count, 2)
        self.assertEqual(result.price_count, 10)
        self.assertEqual(result.applied_price_count, 0)
        self.assertEqual(result.replayed_interval_count, 2)
        self.assertTrue(result.outer_artifact_replayed)
        self.assertTrue(result.completion_replayed)
        self.assertEqual(
            operations,
            ["fetch", "record:1", "extract", "ingest:2:100", "ingest:3:101", "complete:4:10"],
        )
        self.assertEqual([connection.close_calls for connection in connections], [1, 1, 1, 1])

    def test_malformed_last_modified_records_sanitized_failure(self) -> None:
        connections: list[Connection] = []
        failures: list[tuple[BackfillClaim, str]] = []
        claim = BackfillClaim(
            "price-run",
            "dispatch_price",
            date(2026, 8, 29),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260829.zip",
            1,
        )

        def connect(unused_url: str, *, connect_timeout: int) -> Connection:
            self.assertEqual(connect_timeout, 10)
            connection = Connection(len(connections) + 1)
            connections.append(connection)
            return connection

        class Ledger:
            def __init__(self, connection: Connection) -> None:
                self.connection = connection

            def fail(self, actual_claim: BackfillClaim, *, error_summary: str) -> Any:
                failures.append((actual_claim, error_summary))

        with self.assertRaisesRegex(ValueError, "Last-Modified"):
            run_price_backfill_claim(
                "postgresql://private",
                claim,
                datetime(2026, 8, 29, 14, tzinfo=UTC),
                datetime(2026, 8, 29, 15, tzinfo=UTC),
                connect=connect,
                fetch=lambda *args, **kwargs: NemwebHttpResource(
                    claim.source_url,
                    claim.source_url,
                    b"outer",
                    "application/zip",
                    None,
                    "not-a-date",
                ),
                ledger_factory=Ledger,
            )

        self.assertEqual(failures, [(claim, "ValueError")])
        self.assertEqual([connection.close_calls for connection in connections], [1])

    def test_invalid_inputs_fail_before_fetch_or_connect(self) -> None:
        claim = BackfillClaim(
            "price-run",
            "dispatch_price",
            date(2026, 8, 29),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260829.zip",
            1,
        )
        calls: list[str] = []

        def forbidden_connect(*args: Any, **kwargs: Any) -> Any:
            calls.append("connect")
            raise AssertionError("invalid input must not connect")

        def forbidden_fetch(*args: Any, **kwargs: Any) -> NemwebHttpResource:
            calls.append("fetch")
            raise AssertionError("invalid input must not fetch")

        for database_url, actual_claim, start, end in (
            ("", claim, datetime(2026, 8, 29, 14, tzinfo=UTC), datetime(2026, 8, 29, 15, tzinfo=UTC)),
            ("postgresql://private", BackfillClaim(claim.run_id, "dispatch_scada", claim.report_date, claim.source_url, 1), datetime(2026, 8, 29, 14, tzinfo=UTC), datetime(2026, 8, 29, 15, tzinfo=UTC)),
            ("postgresql://private", claim, datetime(2026, 8, 29, 14), datetime(2026, 8, 29, 15, tzinfo=UTC)),
            ("postgresql://private", claim, datetime(2026, 8, 29, 15, tzinfo=UTC), datetime(2026, 8, 29, 15, tzinfo=UTC)),
        ):
            with self.subTest(database_url=database_url, feed=actual_claim.feed, start=start):
                with self.assertRaises(ValueError):
                    run_price_backfill_claim(
                        database_url,
                        actual_claim,
                        start,
                        end,
                        connect=forbidden_connect,
                        fetch=forbidden_fetch,
                    )
        self.assertEqual(calls, [])

    def test_zero_selected_intervals_records_failure_without_completion(self) -> None:
        connections: list[Connection] = []
        failures: list[str] = []
        claim = BackfillClaim(
            "price-run",
            "dispatch_price",
            date(2026, 8, 29),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260829.zip",
            1,
        )
        outer = NemwebOuterArchiveArtifact(
            DISPATCHIS_PRICE_FEED,
            claim.report_date,
            claim.source_url,
            "PUBLIC_DISPATCHIS_20260829.zip",
            "a" * 64,
            b"outer",
        )

        def connect(unused_url: str, *, connect_timeout: int) -> Connection:
            connection = Connection(len(connections) + 1)
            connections.append(connection)
            return connection

        class Registrar:
            def __init__(self, connection: Connection) -> None:
                self.connection = connection

            def record(self, receipt: Any) -> BackfillArtifactResult:
                return BackfillArtifactResult("a" * 64, len(receipt.raw_archive), False)

        class Ledger:
            def __init__(self, connection: Connection) -> None:
                self.connection = connection

            def fail(self, actual_claim: BackfillClaim, *, error_summary: str) -> Any:
                failures.append(error_summary)

            def complete(self, *args: Any, **kwargs: Any) -> Any:
                raise AssertionError("empty archive must not complete")

        with self.assertRaisesRegex(ValueError, "selected no intervals"):
            run_price_backfill_claim(
                "postgresql://private",
                claim,
                datetime(2026, 8, 29, 14, tzinfo=UTC),
                datetime(2026, 8, 29, 15, tzinfo=UTC),
                connect=connect,
                fetch=lambda *args, **kwargs: NemwebHttpResource(
                    claim.source_url, claim.source_url, b"outer", None, None, None
                ),
                registrar_factory=Registrar,
                ledger_factory=Ledger,
                extract_archive=lambda *args, **kwargs: NemwebArchiveExtraction(outer, ()),
            )

        self.assertEqual(failures, ["ValueError"])
        self.assertEqual([connection.close_calls for connection in connections], [1, 1])


if __name__ == "__main__":
    unittest.main()
