"""Tests for two-pass authoritative Next Day monthly backfill claims."""

from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest

from batterywatch_api.backfill_artifacts import BackfillArtifactResult
from batterywatch_api.backfill_ledger import BackfillClaim, BackfillItemCompletion
from batterywatch_api.battery_assets import load_battery_assets
from batterywatch_api.historical_nextday_backfill import (
    HistoricalNextDayBackfillResult,
    run_nextday_soc_backfill_claim,
)
from batterywatch_api.nemweb_http import NemwebHttpResource
from batterywatch_api.nested_source_artifacts import NestedSourceArtifactResult
from batterywatch_api.nextday_archives import (
    NEXTDAY_ARCHIVE_INDEX_URL,
    NextDayMonthlyArchiveRef,
)
from batterywatch_api.nextday_monthly_extraction import (
    NextDayDailyArtifact,
    NextDayDailyMemberRef,
    NextDayMonthlyArchiveManifest,
)
from batterywatch_api.nextday_soc_ingestion import NextDaySocIngestionResult

UTC = timezone.utc
OUTER_URL = (
    NEXTDAY_ARCHIVE_INDEX_URL + "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip"
)
ASSETS = load_battery_assets(
    Path(__file__).resolve().parents[2] / "config/battery_assets.json"
)


class FakeConnection:
    def close(self) -> None:
        pass


class FakeLedger:
    failures: list[tuple[str, str]] = []
    completions: list[tuple[str, BackfillItemCompletion]] = []

    def __init__(self, connection) -> None:
        pass

    def fail(self, claim, *, error_summary):
        self.failures.append((claim.run_id, error_summary))
        return SimpleNamespace(replayed=False)

    def complete(self, claim, completion):
        self.completions.append((claim.run_id, completion))
        return SimpleNamespace(replayed=False)


class FakeOuterRegistrar:
    replayed = False

    def __init__(self, connection) -> None:
        pass

    def record(self, receipt):
        return BackfillArtifactResult(
            "a" * 64, len(receipt.raw_archive), self.replayed
        )


class FakeNestedRegistrar:
    receipts = []
    replayed = False

    def __init__(self, connection) -> None:
        pass

    def record(self, receipt):
        self.receipts.append(receipt)
        digest = hashlib.sha256(receipt.raw_bytes).hexdigest()
        return NestedSourceArtifactResult(
            digest, len(receipt.raw_bytes), self.replayed
        )


def _member(day: int) -> NextDayDailyMemberRef:
    publication_id = f"{47_000_000 + day:016d}"
    return NextDayDailyMemberRef(
        date(2025, 7, day),
        f"PUBLIC_NEXT_DAY_DISPATCH_202507{day:02d}_{publication_id}.zip",
        publication_id,
        datetime(2025, 7, day, 18, 11, tzinfo=UTC),
        10,
        20,
        day,
    )


class HistoricalNextDayBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeLedger.failures = []
        FakeLedger.completions = []
        FakeOuterRegistrar.replayed = False
        FakeNestedRegistrar.receipts = []
        FakeNestedRegistrar.replayed = False

    def test_validates_all_selected_daily_reports_before_first_ingest(self) -> None:
        raw_outer = b"monthly outer"
        reference = NextDayMonthlyArchiveRef(
            date(2025, 7, 1),
            "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
            OUTER_URL,
            len(raw_outer),
            datetime(2026, 8, 30, tzinfo=UTC),
        )
        members = (_member(1), _member(2), _member(3))
        manifest = NextDayMonthlyArchiveManifest(reference, "a" * 64, members)
        parser_calls: list[str] = []
        ingestor_calls: list[object] = []

        def fetch(url, *, max_bytes):
            self.assertEqual(url, OUTER_URL)
            return NemwebHttpResource(url, url, raw_outer, "application/zip", None, None)

        def validate(ref, payload):
            self.assertEqual((ref, payload), (reference, raw_outer))
            return manifest

        def read_daily(received_manifest, payload, member):
            self.assertEqual((received_manifest, payload), (manifest, raw_outer))
            raw = f"zip-{member.report_date.day}".encode()
            csv_bytes = f"day-{member.report_date.day}".encode()
            return NextDayDailyArtifact(
                member,
                hashlib.sha256(raw).hexdigest(),
                raw,
                member.filename.removesuffix(".zip") + ".CSV",
                csv_bytes,
            )

        def parse(payload, **kwargs):
            parser_calls.append(payload)
            if payload == "day-3":
                raise ValueError("bad final daily report")
            return (SimpleNamespace(interval_start=datetime(2025, 7, 2, tzinfo=UTC)),)

        def ingestor_factory(connection):
            ingestor_calls.append(connection)
            return SimpleNamespace(ingest=lambda observations, assets: None)

        claim = BackfillClaim(
            "soc-run-202507",
            "nextday_soc",
            date(2025, 7, 1),
            OUTER_URL,
            1,
        )
        with self.assertRaisesRegex(ValueError, "bad final daily report"):
            run_nextday_soc_backfill_claim(
                "postgresql://redacted",
                ASSETS,
                claim,
                datetime(2025, 7, 1, tzinfo=UTC),
                datetime(2025, 7, 3, tzinfo=UTC),
                ingestion_version=3,
                connect=lambda *args, **kwargs: FakeConnection(),
                fetch=fetch,
                clock=lambda: datetime(2026, 8, 30, tzinfo=UTC),
                ledger_factory=FakeLedger,
                registrar_factory=FakeOuterRegistrar,
                nested_registrar_factory=FakeNestedRegistrar,
                validate_archive=validate,
                read_daily=read_daily,
                parse_csv=parse,
                ingestor_factory=ingestor_factory,
            )

        self.assertEqual(parser_calls, ["day-1", "day-2", "day-3"])
        self.assertEqual(len(FakeNestedRegistrar.receipts), 3)
        self.assertEqual(ingestor_calls, [])
        self.assertEqual(FakeLedger.failures, [(claim.run_id, "ValueError")])

    def test_revalidates_then_ingests_each_day_and_completes_claim(self) -> None:
        raw_outer = b"monthly outer"
        downloaded_at = datetime(2026, 8, 30, tzinfo=UTC)
        reference = NextDayMonthlyArchiveRef(
            date(2025, 7, 1),
            "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
            OUTER_URL,
            len(raw_outer),
            downloaded_at,
        )
        members = (_member(1), _member(2))
        manifest = NextDayMonthlyArchiveManifest(reference, "a" * 64, members)
        parse_calls: list[tuple[str, int]] = []
        read_calls: list[int] = []
        ingest_calls: list[tuple[object, ...]] = []

        def fetch(url, *, max_bytes):
            return NemwebHttpResource(
                url, url, raw_outer, "application/zip", None, None
            )

        def read_daily(received_manifest, payload, member):
            read_calls.append(member.report_date.day)
            raw = f"zip-{member.report_date.day}".encode()
            return NextDayDailyArtifact(
                member,
                hashlib.sha256(raw).hexdigest(),
                raw,
                member.filename.removesuffix(".zip") + ".CSV",
                f"day-{member.report_date.day}".encode(),
            )

        def parse(payload, **kwargs):
            parse_calls.append((payload, kwargs["correction_version"]))
            day = int(payload[-1])
            return (
                SimpleNamespace(
                    interval_start=datetime(2025, 7, day, 12, tzinfo=UTC)
                ),
            )

        class FakeIngestor:
            def __init__(self, connection):
                pass

            def ingest(self, observations, assets):
                materialized = tuple(observations)
                ingest_calls.append(materialized)
                return NextDaySocIngestionResult(1, 1, 0, 1, 1, 0, 0, 1)

        claim = BackfillClaim(
            "soc-run-202507",
            "nextday_soc",
            date(2025, 7, 1),
            OUTER_URL,
            1,
        )
        result = run_nextday_soc_backfill_claim(
            "postgresql://redacted",
            ASSETS,
            claim,
            datetime(2025, 7, 1, tzinfo=UTC),
            datetime(2025, 7, 3, tzinfo=UTC),
            ingestion_version=3,
            connect=lambda *args, **kwargs: FakeConnection(),
            fetch=fetch,
            clock=lambda: downloaded_at,
            ledger_factory=FakeLedger,
            registrar_factory=FakeOuterRegistrar,
            nested_registrar_factory=FakeNestedRegistrar,
            validate_archive=lambda ref, payload: manifest,
            read_daily=read_daily,
            parse_csv=parse,
            ingestor_factory=FakeIngestor,
        )

        self.assertEqual(read_calls, [1, 2, 1, 2])
        self.assertEqual(
            parse_calls,
            [
                ("day-1", 47_000_001),
                ("day-2", 47_000_002),
                ("day-1", 47_000_001),
                ("day-2", 47_000_002),
            ],
        )
        self.assertEqual([len(call) for call in ingest_calls], [1, 1])
        self.assertEqual(
            FakeLedger.completions,
            [(claim.run_id, BackfillItemCompletion(False, 2))],
        )
        self.assertEqual(FakeLedger.failures, [])
        self.assertEqual(
            result,
            HistoricalNextDayBackfillResult(
                "a" * 64, False, 2, 0, 2, 2, 0, 2, 2, 0, 0, 2
            ),
        )

    def test_exact_replay_is_classified_deterministically(self) -> None:
        raw_outer = b"monthly outer"
        downloaded_at = datetime(2026, 8, 30, tzinfo=UTC)
        member = _member(1)
        reference = NextDayMonthlyArchiveRef(
            date(2025, 7, 1),
            "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
            OUTER_URL,
            len(raw_outer),
            downloaded_at,
        )
        manifest = NextDayMonthlyArchiveManifest(
            reference, "a" * 64, (member,)
        )
        FakeOuterRegistrar.replayed = True
        FakeNestedRegistrar.replayed = True

        def read_daily(received_manifest, payload, received_member):
            raw = b"zip-1"
            return NextDayDailyArtifact(
                received_member,
                hashlib.sha256(raw).hexdigest(),
                raw,
                received_member.filename.removesuffix(".zip") + ".CSV",
                b"day-1",
            )

        class ReplayIngestor:
            def __init__(self, connection):
                pass

            def ingest(self, observations, assets):
                return NextDaySocIngestionResult(1, 0, 1, 1, 0, 1, 0, 1)

        claim = BackfillClaim(
            "soc-replay-202507",
            "nextday_soc",
            date(2025, 7, 1),
            OUTER_URL,
            2,
        )
        result = run_nextday_soc_backfill_claim(
            "postgresql://redacted",
            ASSETS,
            claim,
            datetime(2025, 7, 1, tzinfo=UTC),
            datetime(2025, 7, 1, 14, tzinfo=UTC),
            ingestion_version=3,
            connect=lambda *args, **kwargs: FakeConnection(),
            fetch=lambda url, **kwargs: NemwebHttpResource(
                url, url, raw_outer, "application/zip", None, None
            ),
            clock=lambda: downloaded_at,
            ledger_factory=FakeLedger,
            registrar_factory=FakeOuterRegistrar,
            nested_registrar_factory=FakeNestedRegistrar,
            validate_archive=lambda ref, payload: manifest,
            read_daily=read_daily,
            parse_csv=lambda payload, **kwargs: (
                SimpleNamespace(
                    interval_start=datetime(2025, 7, 1, 12, tzinfo=UTC)
                ),
            ),
            ingestor_factory=ReplayIngestor,
        )

        self.assertEqual(
            FakeLedger.completions,
            [(claim.run_id, BackfillItemCompletion(True, 1))],
        )
        self.assertEqual(
            result,
            HistoricalNextDayBackfillResult(
                "a" * 64, True, 1, 1, 1, 0, 1, 1, 0, 1, 0, 1
            ),
        )

        class IncompleteReplayIngestor:
            def __init__(self, connection):
                pass

            def ingest(self, observations, assets):
                return NextDaySocIngestionResult(1, 0, 0, 1, 0, 0, 0, 0)

        FakeLedger.completions = []
        partial_claim = BackfillClaim(
            "soc-partial-replay-202507",
            "nextday_soc",
            date(2025, 7, 1),
            OUTER_URL,
            3,
        )
        run_nextday_soc_backfill_claim(
            "postgresql://redacted",
            ASSETS,
            partial_claim,
            datetime(2025, 7, 1, tzinfo=UTC),
            datetime(2025, 7, 1, 14, tzinfo=UTC),
            ingestion_version=3,
            connect=lambda *args, **kwargs: FakeConnection(),
            fetch=lambda url, **kwargs: NemwebHttpResource(
                url, url, raw_outer, "application/zip", None, None
            ),
            clock=lambda: downloaded_at,
            ledger_factory=FakeLedger,
            registrar_factory=FakeOuterRegistrar,
            nested_registrar_factory=FakeNestedRegistrar,
            validate_archive=lambda ref, payload: manifest,
            read_daily=read_daily,
            parse_csv=lambda payload, **kwargs: (
                SimpleNamespace(
                    interval_start=datetime(2025, 7, 1, 12, tzinfo=UTC)
                ),
            ),
            ingestor_factory=IncompleteReplayIngestor,
        )
        self.assertEqual(
            FakeLedger.completions,
            [(partial_claim.run_id, BackfillItemCompletion(False, 1))],
        )

    def test_fails_closed_when_range_selects_no_daily_member(self) -> None:
        raw_outer = b"monthly outer"
        downloaded_at = datetime(2026, 8, 30, tzinfo=UTC)
        reference = NextDayMonthlyArchiveRef(
            date(2025, 7, 1),
            "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
            OUTER_URL,
            len(raw_outer),
            downloaded_at,
        )
        manifest = NextDayMonthlyArchiveManifest(
            reference, "a" * 64, (_member(2),)
        )
        claim = BackfillClaim(
            "soc-empty-202507",
            "nextday_soc",
            date(2025, 7, 1),
            OUTER_URL,
            1,
        )

        with self.assertRaisesRegex(ValueError, "no daily"):
            run_nextday_soc_backfill_claim(
                "postgresql://redacted",
                ASSETS,
                claim,
                datetime(2025, 7, 1, tzinfo=UTC),
                datetime(2025, 7, 1, 1, tzinfo=UTC),
                ingestion_version=3,
                connect=lambda *args, **kwargs: FakeConnection(),
                fetch=lambda url, **kwargs: NemwebHttpResource(
                    url, url, raw_outer, "application/zip", None, None
                ),
                clock=lambda: downloaded_at,
                ledger_factory=FakeLedger,
                registrar_factory=FakeOuterRegistrar,
                validate_archive=lambda ref, payload: manifest,
                read_daily=lambda *args: self.fail("must not read an unselected day"),
            )

        self.assertEqual(FakeLedger.completions, [])
        self.assertEqual(FakeLedger.failures, [(claim.run_id, "ValueError")])

    def test_maps_nem_0400_interval_to_previous_report_date(self) -> None:
        raw_outer = b"monthly outer"
        downloaded_at = datetime(2026, 8, 30, tzinfo=UTC)
        reference = NextDayMonthlyArchiveRef(
            date(2025, 7, 1),
            "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
            OUTER_URL,
            len(raw_outer),
            downloaded_at,
        )
        members = (_member(1), _member(2))
        manifest = NextDayMonthlyArchiveManifest(reference, "a" * 64, members)
        ingested: list[tuple[object, ...]] = []

        def read_daily(received_manifest, payload, member):
            raw = f"zip-{member.report_date.day}".encode()
            return NextDayDailyArtifact(
                member,
                hashlib.sha256(raw).hexdigest(),
                raw,
                member.filename.removesuffix(".zip") + ".CSV",
                f"day-{member.report_date.day}".encode(),
            )

        def parse(payload, **kwargs):
            day = int(payload[-1])
            interval = datetime(2025, 7, 1, 18, 0, tzinfo=UTC)
            if day == 2:
                interval = datetime(2025, 7, 1, 18, 5, tzinfo=UTC)
            return (SimpleNamespace(interval_start=interval),)

        class BoundaryIngestor:
            def __init__(self, connection):
                pass

            def ingest(self, observations, assets):
                materialized = tuple(observations)
                ingested.append(materialized)
                return NextDaySocIngestionResult(1, 1, 0, 1, 1, 0, 0, 1)

        claim = BackfillClaim(
            "soc-boundary-202507",
            "nextday_soc",
            date(2025, 7, 1),
            OUTER_URL,
            1,
        )
        run_nextday_soc_backfill_claim(
            "postgresql://redacted",
            ASSETS,
            claim,
            datetime(2025, 7, 1, 18, 0, tzinfo=UTC),
            datetime(2025, 7, 1, 18, 1, tzinfo=UTC),
            ingestion_version=3,
            connect=lambda *args, **kwargs: FakeConnection(),
            fetch=lambda url, **kwargs: NemwebHttpResource(
                url, url, raw_outer, "application/zip", None, None
            ),
            clock=lambda: downloaded_at,
            ledger_factory=FakeLedger,
            registrar_factory=FakeOuterRegistrar,
            nested_registrar_factory=FakeNestedRegistrar,
            validate_archive=lambda ref, payload: manifest,
            read_daily=read_daily,
            parse_csv=parse,
            ingestor_factory=BoundaryIngestor,
        )

        self.assertEqual(
            [receipt.filename for receipt in FakeNestedRegistrar.receipts],
            [members[0].filename],
        )
        self.assertEqual([len(rows) for rows in ingested], [1])
        self.assertEqual(
            FakeLedger.completions,
            [(claim.run_id, BackfillItemCompletion(False, 1))],
        )


if __name__ == "__main__":
    unittest.main()
