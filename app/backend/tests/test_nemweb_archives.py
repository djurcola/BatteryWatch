"""Tests for the bounded NEMWeb historical archive seam."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from batterywatch_api.aemo import parse_dispatch_price_mms_csv
from batterywatch_api.nemweb_archives import (
    ARCHIVE_SOURCE,
    CURRENT_INDEX_SOURCE,
    DISPATCHIS_PRICE_FEED,
    DISPATCH_SCADA_FEED,
    MAX_NESTED_RECURSION_DEPTH,
    ArchivePlanItem,
    NemwebArchiveError,
    extract_nested_archive,
    extract_nested_archive_tree,
    plan_archive_range,
)


UTC = timezone.utc


class ArchiveRangePlannerTests(unittest.TestCase):
    def test_plans_intersecting_nem_dates_and_current_fallback_deterministically(self) -> None:
        plan = plan_archive_range(
            datetime(2026, 8, 29, 14, 30, tzinfo=UTC),
            datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
            feeds=(DISPATCH_SCADA_FEED, DISPATCHIS_PRICE_FEED),
            archived_dates={
                DISPATCH_SCADA_FEED: (date(2026, 8, 30),),
                DISPATCHIS_PRICE_FEED: (date(2026, 8, 30),),
            },
            current_index_dates={
                DISPATCH_SCADA_FEED: (date(2026, 8, 31), date(2026, 9, 1)),
                DISPATCHIS_PRICE_FEED: (date(2026, 8, 31), date(2026, 9, 1)),
            },
        )

        self.assertEqual(
            tuple(
                (item.feed, item.report_date, item.source, item.url)
                for item in plan.items
            ),
            (
                (
                    DISPATCH_SCADA_FEED,
                    date(2026, 8, 30),
                    ARCHIVE_SOURCE,
                    "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/PUBLIC_DISPATCHSCADA_20260830.zip",
                ),
                (
                    DISPATCHIS_PRICE_FEED,
                    date(2026, 8, 30),
                    ARCHIVE_SOURCE,
                    "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/PUBLIC_DISPATCHIS_20260830.zip",
                ),
                (
                    DISPATCH_SCADA_FEED,
                    date(2026, 8, 31),
                    CURRENT_INDEX_SOURCE,
                    "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/",
                ),
                (
                    DISPATCHIS_PRICE_FEED,
                    date(2026, 8, 31),
                    CURRENT_INDEX_SOURCE,
                    "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/",
                ),
                (
                    DISPATCH_SCADA_FEED,
                    date(2026, 9, 1),
                    CURRENT_INDEX_SOURCE,
                    "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/",
                ),
                (
                    DISPATCHIS_PRICE_FEED,
                    date(2026, 9, 1),
                    CURRENT_INDEX_SOURCE,
                    "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/",
                ),
            ),
        )
        self.assertEqual(plan.start, datetime(2026, 8, 29, 14, 30, tzinfo=UTC))
        self.assertEqual(plan.end, datetime(2026, 8, 31, 14, 30, tzinfo=UTC))

    def test_midnight_start_includes_previous_market_day_archive(self) -> None:
        plan = plan_archive_range(
            datetime(2026, 8, 29, 14, 0, tzinfo=UTC),
            datetime(2026, 8, 30, 14, 0, tzinfo=UTC),
            feeds=(DISPATCH_SCADA_FEED,),
            archived_dates={
                DISPATCH_SCADA_FEED: (date(2026, 8, 29), date(2026, 8, 30)),
            },
        )

        self.assertEqual(
            tuple(item.report_date for item in plan.items),
            (date(2026, 8, 29), date(2026, 8, 30)),
        )

    def test_rejects_unsupported_unavailable_and_excessive_plans(self) -> None:
        start = datetime(2026, 8, 30, tzinfo=UTC)
        end = datetime(2026, 8, 31, tzinfo=UTC)
        with self.assertRaises(NemwebArchiveError):
            plan_archive_range(start, end, feeds=("unknown",), archived_dates={})
        with self.assertRaises(NemwebArchiveError):
            plan_archive_range(start, end, feeds=(DISPATCH_SCADA_FEED,), archived_dates={})
        with self.assertRaises(NemwebArchiveError):
            plan_archive_range(
                start, end + timedelta(days=1),
                feeds=(DISPATCH_SCADA_FEED,),
                archived_dates={
                    DISPATCH_SCADA_FEED: (date(2026, 8, 30), date(2026, 8, 31))
                },
                max_artifacts=1,
            )

    def test_rejects_a_plan_spanning_more_than_366_report_dates(self) -> None:
        report_dates = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(367))
        with self.assertRaises(NemwebArchiveError):
            plan_archive_range(
                datetime(2026, 1, 1, 12, tzinfo=UTC),
                datetime(2027, 1, 2, 11, 59, tzinfo=UTC),
                feeds=(DISPATCH_SCADA_FEED,),
                archived_dates={DISPATCH_SCADA_FEED: report_dates},
            )

    def test_rejects_invalid_and_excessive_ranges(self) -> None:
        valid_dates = {DISPATCH_SCADA_FEED: (date(2026, 8, 30),)}
        cases: tuple[tuple[Any, Any], ...] = (
            (datetime(2026, 8, 30), datetime(2026, 8, 30, 1)),
            (datetime(2026, 8, 30, tzinfo=UTC), datetime(2026, 8, 30, tzinfo=UTC)),
            (datetime(2026, 8, 30, tzinfo=UTC), datetime(2026, 8, 29, 23, tzinfo=UTC)),
            (datetime(2026, 8, 30, tzinfo=UTC), datetime(2027, 9, 1, tzinfo=UTC)),
        )
        for start, end in cases:
            with self.subTest(start=start, end=end), self.assertRaises(NemwebArchiveError):
                plan_archive_range(
                    start, end, feeds=(DISPATCH_SCADA_FEED,),
                    archived_dates=valid_dates,
                )
        with self.assertRaises(NemwebArchiveError):
            plan_archive_range(
                datetime(2026, 8, 30, tzinfo=UTC),
                datetime(2026, 8, 31, tzinfo=UTC),
                feeds=(DISPATCH_SCADA_FEED,),
                archived_dates=valid_dates,
                max_artifacts=0,
            )


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "historical"
_REPORT_DATE = date(2026, 8, 30)
_SCADA_SOURCE_ID = "0000000535210764"
_SCADA_MEMBER = "PUBLIC_DISPATCHSCADA_202608301455_0000000535210764.zip"
_SCADA_URL = (
    "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
    "PUBLIC_DISPATCHSCADA_20260830.zip"
)
_PRICE_SOURCE_ID = "0000000535211318"
_PRICE_MEMBER = "PUBLIC_DISPATCHIS_202608301500_0000000535211318.zip"
_PRICE_URL = (
    "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
    "PUBLIC_DISPATCHIS_20260830.zip"
)


def _canonical_fixture_csv(filename: str, source_id: str, table: str) -> bytes:
    lines = (
        (_FIXTURE_DIR / filename).read_text(encoding="utf-8").splitlines()
    )
    footer = lines[-1].split(",")
    footer[2] = str(len(lines) + 1)
    metadata = (
        f"C,NEMP.WORLD,{table},AEMO,PUBLIC,2026/08/30,15:00:15,"
        f"{source_id},{table},0000000000000000"
    )
    return ("\n".join((metadata, *lines[:-1], ",".join(footer))) + "\n").encode(
        "utf-8"
    )


def _inner_zip(member_name: str, csv_payload: bytes) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(member_name.removesuffix(".zip") + ".CSV", csv_payload)
    return buffer.getvalue()


def _scada_csv_at(local_timestamp: str, source_id: str) -> bytes:
    payload = _canonical_fixture_csv(
        "dispatch-scada-20260830-1455-reduced.csv", source_id, "DISPATCHSCADA"
    )
    return payload.replace(b"2026/08/30 14:55:00", local_timestamp.encode())


def _inner_zip_with_extra(member_name: str, csv_payload: bytes) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(member_name.removesuffix(".zip") + ".CSV", csv_payload)
        archive.writestr("unexpected.txt", b"extra")
    return buffer.getvalue()


def _outer_zip(member_name: str, inner_payload: bytes) -> bytes:
    return _outer_zip_members(((member_name, inner_payload),))


def _outer_zip_members(
    members: tuple[tuple[str, bytes], ...], *, compression: int = ZIP_DEFLATED
) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=compression) as archive:
        for member_name, inner_payload in members:
            archive.writestr(member_name, inner_payload)
    return buffer.getvalue()


def _encrypted_outer_zip(member_name: str, inner_payload: bytes) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(member_name, inner_payload)
    return _flag_encrypted(buffer.getvalue())


def _flag_encrypted(payload: bytes) -> bytes:
    data = bytearray(payload)
    assert data[:4] == b"PK\x03\x04"
    data[6] |= 1
    central = data.rindex(b"PK\x01\x02")
    data[central + 8] |= 1
    return bytes(data)


class NestedArchiveExtractionTests(unittest.TestCase):
    def test_accepts_following_midnight_as_market_day_final_interval(self) -> None:
        source_id = "0000000535210765"
        member = f"PUBLIC_DISPATCHSCADA_202608310000_{source_id}.zip"
        csv_payload = _scada_csv_at("2026/08/31 00:00:00", source_id)
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )

        extraction = extract_nested_archive(
            item,
            _outer_zip(member, _inner_zip(member, csv_payload)),
            allow_reduced=True,
        )

        self.assertEqual(
            extraction.nested[0].interval_timestamp,
            datetime(2026, 8, 30, 14, 0, tzinfo=UTC),
        )

    def test_rejects_same_day_midnight_outside_market_day_archive(self) -> None:
        source_id = "0000000535210765"
        member = f"PUBLIC_DISPATCHSCADA_202608300000_{source_id}.zip"
        csv_payload = _scada_csv_at("2026/08/30 00:00:00", source_id)
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )

        with self.assertRaises(NemwebArchiveError):
            extract_nested_archive(
                item,
                _outer_zip(member, _inner_zip(member, csv_payload)),
                allow_reduced=True,
            )

    def test_complete_market_day_sequence_starts_at_five_minutes(self) -> None:
        first_id = "0000000535210001"
        second_id = "0000000535210002"
        first = f"PUBLIC_DISPATCHSCADA_202608300005_{first_id}.zip"
        second = f"PUBLIC_DISPATCHSCADA_202608300010_{second_id}.zip"
        outer_payload = _outer_zip_members((
            (first, _inner_zip(first, _scada_csv_at("2026/08/30 00:05:00", first_id))),
            (second, _inner_zip(second, _scada_csv_at("2026/08/30 00:10:00", second_id))),
        ))
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )

        with patch("batterywatch_api.nemweb_archives.MAX_DAILY_INTERVALS", 2):
            extraction = extract_nested_archive(item, outer_payload)

        self.assertEqual(
            tuple(artifact.interval_timestamp for artifact in extraction.nested),
            (
                datetime(2026, 8, 29, 14, 5, tzinfo=UTC),
                datetime(2026, 8, 29, 14, 10, tzinfo=UTC),
            ),
        )

    def test_production_mode_rejects_reduced_archive(self) -> None:
        csv_payload = _canonical_fixture_csv(
            "dispatch-scada-20260830-1455-reduced.csv",
            _SCADA_SOURCE_ID,
            "DISPATCHSCADA",
        )
        outer_payload = _outer_zip(
            _SCADA_MEMBER,
            _inner_zip(_SCADA_MEMBER, csv_payload),
        )
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )
        with self.assertRaises(NemwebArchiveError):
            extract_nested_archive(item, outer_payload)

    def test_rejects_wrong_outer_provenance(self) -> None:
        csv_payload = _canonical_fixture_csv(
            "dispatch-scada-20260830-1455-reduced.csv",
            _SCADA_SOURCE_ID,
            "DISPATCHSCADA",
        )
        outer_payload = _outer_zip(
            _SCADA_MEMBER,
            _inner_zip(_SCADA_MEMBER, csv_payload),
        )
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )
        cases: tuple[tuple[ArchivePlanItem, dict[str, Any]], ...] = (
            (item, {"outer_filename": "PUBLIC_DISPATCHSCADA_20260831.zip"}),
            (item, {"outer_url": _SCADA_URL.replace("20260830", "20260831")}),
            (item, {"outer_url": "https://example.invalid/archive.zip"}),
            (ArchivePlanItem(
                DISPATCH_SCADA_FEED, _REPORT_DATE, ARCHIVE_SOURCE,
                "https://example.invalid/archive.zip",
            ), {"outer_url": _SCADA_URL}),
        )
        for bad_item, kwargs in cases:
            with self.subTest(item=bad_item, kwargs=kwargs), self.assertRaises(NemwebArchiveError):
                extract_nested_archive(bad_item, outer_payload, allow_reduced=True, **kwargs)

    def test_rejects_wrong_inner_feed_date_and_timestamp(self) -> None:
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )
        cases = (
            "PUBLIC_DISPATCHIS_202608301455_0000000535210764.zip",
            "PUBLIC_DISPATCHSCADA_202608311455_0000000535210764.zip",
            "PUBLIC_DISPATCHSCADA_202608301456_0000000535210764.zip",
        )
        for member_name in cases:
            with self.subTest(member_name=member_name), self.assertRaises(NemwebArchiveError):
                extract_nested_archive(
                    item, _outer_zip(member_name, b"not an inner zip"),
                    allow_reduced=True,
                )

    def test_rejects_wrong_inner_csv_provenance(self) -> None:
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )
        valid = _canonical_fixture_csv(
            "dispatch-scada-20260830-1455-reduced.csv",
            _SCADA_SOURCE_ID,
            "DISPATCHSCADA",
        )
        cases = (
            _canonical_fixture_csv(
                "dispatch-scada-20260830-1455-reduced.csv",
                _SCADA_SOURCE_ID,
                "WRONGFEED",
            ),
            valid.replace(b"2026/08/30 14:55:00", b"2026/08/30 15:00:00"),
            valid.replace(b"2026/08/30 14:55:00", b"2026/08/31 14:55:00"),
            _canonical_fixture_csv(
                "dispatch-scada-20260830-1455-reduced.csv",
                "0000000535210765",
                "DISPATCHSCADA",
            ),
        )
        for csv_payload in cases:
            with self.subTest(payload=csv_payload[:30]), self.assertRaises(NemwebArchiveError):
                extract_nested_archive(
                    item, _outer_zip(_SCADA_MEMBER, _inner_zip(_SCADA_MEMBER, csv_payload)),
                    allow_reduced=True,
                )

    def test_rejects_unsafe_or_malformed_nested_members(self) -> None:
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )
        valid_name = _SCADA_MEMBER
        cases = (
            _outer_zip("../" + valid_name, b"not a zip"),
            _outer_zip(valid_name + "/", b"not a zip"),
            _encrypted_outer_zip(valid_name, b"not a zip"),
            _outer_zip_members(((valid_name, b"not a zip"),), compression=ZIP_BZIP2),
            _outer_zip(valid_name, b"not a zip"),
        )
        for outer_payload in cases:
            with self.subTest(size=len(outer_payload)), self.assertRaises(NemwebArchiveError):
                extract_nested_archive(item, outer_payload, allow_reduced=True)

    def test_rejects_duplicate_source_ids_and_timestamps(self) -> None:
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )
        cases = (
            (
                (_SCADA_MEMBER, b"x"),
                ("PUBLIC_DISPATCHSCADA_202608301500_0000000535210764.zip", b"x"),
            ),
            (
                (_SCADA_MEMBER, b"x"),
                ("PUBLIC_DISPATCHSCADA_202608301455_0000000535210765.zip", b"x"),
            ),
        )
        for members in cases:
            with self.subTest(members=members), self.assertRaises(NemwebArchiveError):
                extract_nested_archive(
                    item, _outer_zip_members(members), allow_reduced=True,
                )

    def test_rejects_nested_compressed_uncompressed_and_ratio_bounds(self) -> None:
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )
        payload = _outer_zip(_SCADA_MEMBER, b"A" * 10000)
        cases = (
            ("MAX_NESTED_ARCHIVE_BYTES", 1),
            ("MAX_NESTED_COMPRESSION_RATIO", 1),
            ("MAX_OUTER_COMPRESSED_BYTES", 0),
            ("MAX_OUTER_UNCOMPRESSED_BYTES", 0),
            ("MAX_OUTER_ARCHIVE_BYTES", len(payload) - 1),
        )
        for constant, value in cases:
            with self.subTest(constant=constant), patch(
                f"batterywatch_api.nemweb_archives.{constant}", value
            ), self.assertRaises(NemwebArchiveError):
                extract_nested_archive(item, payload, allow_reduced=True)

    def test_rejects_unexpected_outer_or_inner_members(self) -> None:
        csv_payload = _canonical_fixture_csv(
            "dispatch-scada-20260830-1455-reduced.csv",
            _SCADA_SOURCE_ID,
            "DISPATCHSCADA",
        )
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )
        cases = (
            _outer_zip_members(((_SCADA_MEMBER, _inner_zip(_SCADA_MEMBER, csv_payload)),
                                ("unexpected.txt", b"extra"))),
            _outer_zip(_SCADA_MEMBER, _inner_zip_with_extra(_SCADA_MEMBER, csv_payload)),
        )
        for payload in cases:
            with self.subTest(size=len(payload)), self.assertRaises(NemwebArchiveError):
                extract_nested_archive(item, payload, allow_reduced=True)

    def test_filters_exact_half_open_range_only_after_full_validation(self) -> None:
        second = "PUBLIC_DISPATCHSCADA_202608301500_0000000535210765.zip"
        first_payload = _inner_zip(
            _SCADA_MEMBER, _scada_csv_at("2026/08/30 14:55:00", _SCADA_SOURCE_ID)
        )
        second_payload = _inner_zip(
            second, _scada_csv_at("2026/08/30 15:00:00", "0000000535210765")
        )
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )
        extraction = extract_nested_archive(
            item,
            _outer_zip_members(((_SCADA_MEMBER, first_payload), (second, second_payload))),
            allow_reduced=True,
            start=datetime(2026, 8, 30, 4, 55, tzinfo=UTC),
            end=datetime(2026, 8, 30, 5, 0, tzinfo=UTC),
        )
        self.assertEqual(
            tuple(artifact.interval_timestamp for artifact in extraction.nested),
            (datetime(2026, 8, 30, 4, 55, tzinfo=UTC),),
        )
        with self.assertRaises(NemwebArchiveError):
            extract_nested_archive(
                item,
                _outer_zip_members((
                    (_SCADA_MEMBER, first_payload), (second, b"bad inner zip")
                )),
                allow_reduced=True,
                start=datetime(2026, 8, 30, 4, 55, tzinfo=UTC),
                end=datetime(2026, 8, 30, 5, 0, tzinfo=UTC),
            )

    def test_parses_authentic_five_region_price_fixture_and_keeps_negative_rrp(self) -> None:
        csv_payload = _canonical_fixture_csv(
            "dispatch-price-20260830-1500-reduced.csv",
            _PRICE_SOURCE_ID,
            "DISPATCHIS",
        )
        records = parse_dispatch_price_mms_csv(
            csv_payload.decode("utf-8"),
            source_id=_PRICE_SOURCE_ID,
            ingestion_version=0,
        )
        self.assertEqual({record.region for record in records}, {"NSW1", "QLD1", "SA1", "TAS1", "VIC1"})
        self.assertEqual(
            next(record for record in records if record.region == "NSW1").price_aud_per_mwh,
            -6.93755,
        )
        item = ArchivePlanItem(
            feed=DISPATCHIS_PRICE_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_PRICE_URL,
        )
        extraction = extract_nested_archive(
            item, _outer_zip(_PRICE_MEMBER, _inner_zip(_PRICE_MEMBER, csv_payload)),
            allow_reduced=True,
        )
        self.assertEqual(extraction.nested[0].interval_timestamp,
                         datetime(2026, 8, 30, 5, 0, tzinfo=UTC))

    def test_extracts_reduced_real_scada_archive_with_outer_provenance(self) -> None:
        csv_payload = _canonical_fixture_csv(
            "dispatch-scada-20260830-1455-reduced.csv",
            _SCADA_SOURCE_ID,
            "DISPATCHSCADA",
        )
        outer_payload = _outer_zip(
            _SCADA_MEMBER,
            _inner_zip(_SCADA_MEMBER, csv_payload),
        )
        item = ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )

        extraction = extract_nested_archive(
            item,
            outer_payload,
            allow_reduced=True,
        )

        self.assertEqual(
            (
                extraction.outer.url,
                extraction.outer.filename,
                extraction.outer.report_date,
                extraction.outer.sha256,
                extraction.outer.raw_bytes,
            ),
            (
                _SCADA_URL,
                "PUBLIC_DISPATCHSCADA_20260830.zip",
                _REPORT_DATE,
                sha256(outer_payload).hexdigest(),
                outer_payload,
            ),
        )
        self.assertEqual(len(extraction.nested), 1)
        nested = extraction.nested[0]
        self.assertEqual(
            (
                nested.member_name,
                nested.source_artifact_id,
                nested.interval_timestamp,
                nested.sha256,
                nested.raw_bytes,
            ),
            (
                _SCADA_MEMBER,
                _SCADA_SOURCE_ID,
                datetime(2026, 8, 30, 4, 55, tzinfo=timezone.utc),
                sha256(_inner_zip(_SCADA_MEMBER, csv_payload)).hexdigest(),
                _inner_zip(_SCADA_MEMBER, csv_payload),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            extraction.outer.url = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            nested.member_name = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            extraction.nested = ()  # type: ignore[misc]


_NEM_TIMEZONE = timezone(timedelta(hours=10))


class ArchivePlannerDeterminismTests(unittest.TestCase):
    def test_plan_is_deterministic_and_independent_of_requested_feed_order(self) -> None:
        archived_dates = {
            DISPATCH_SCADA_FEED: (date(2026, 8, 30), date(2026, 8, 31)),
            DISPATCHIS_PRICE_FEED: (date(2026, 8, 30), date(2026, 8, 31)),
        }
        first = plan_archive_range(
            datetime(2026, 8, 29, 14, 30, tzinfo=UTC),
            datetime(2026, 8, 30, 14, 30, tzinfo=UTC),
            feeds=(DISPATCH_SCADA_FEED, DISPATCHIS_PRICE_FEED),
            archived_dates=archived_dates,
        )
        second = plan_archive_range(
            datetime(2026, 8, 29, 14, 30, tzinfo=UTC),
            datetime(2026, 8, 30, 14, 30, tzinfo=UTC),
            feeds=(DISPATCHIS_PRICE_FEED, DISPATCH_SCADA_FEED),
            archived_dates=archived_dates,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            tuple((item.feed, item.report_date) for item in first.items),
            (
                (DISPATCH_SCADA_FEED, date(2026, 8, 30)),
                (DISPATCHIS_PRICE_FEED, date(2026, 8, 30)),
                (DISPATCH_SCADA_FEED, date(2026, 8, 31)),
                (DISPATCHIS_PRICE_FEED, date(2026, 8, 31)),
            ),
        )

    def test_normalizes_non_utc_aware_bounds_to_utc(self) -> None:
        plan = plan_archive_range(
            datetime(2026, 8, 30, 0, 30, tzinfo=_NEM_TIMEZONE),
            datetime(2026, 8, 31, 0, 30, tzinfo=_NEM_TIMEZONE),
            feeds=(DISPATCH_SCADA_FEED,),
            archived_dates={
                DISPATCH_SCADA_FEED: (date(2026, 8, 30), date(2026, 8, 31)),
            },
        )
        self.assertEqual(
            (plan.start, plan.end),
            (
                datetime(2026, 8, 29, 14, 30, tzinfo=UTC),
                datetime(2026, 8, 30, 14, 30, tzinfo=UTC),
            ),
        )

    def test_rejects_empty_non_string_and_duplicate_feeds(self) -> None:
        start = datetime(2026, 8, 30, tzinfo=UTC)
        end = datetime(2026, 8, 31, tzinfo=UTC)
        cases: tuple[Any, ...] = ((), (5,), (DISPATCH_SCADA_FEED, DISPATCH_SCADA_FEED))
        for feeds in cases:
            with self.subTest(feeds=feeds), self.assertRaises(NemwebArchiveError):
                plan_archive_range(start, end, feeds=feeds, archived_dates={})

    def test_rejects_non_datetime_bounds_and_invalid_artifact_limits(self) -> None:
        valid_dates = {DISPATCH_SCADA_FEED: (date(2026, 8, 30),)}
        cases: tuple[Any, ...] = (
            (None, datetime(2026, 8, 31, tzinfo=UTC)),
            ("2026-08-30T00:00:00Z", datetime(2026, 8, 31, tzinfo=UTC)),
            (datetime(2026, 8, 30, tzinfo=UTC), None),
        )
        for start, end in cases:
            with self.subTest(start=start, end=end), self.assertRaises(NemwebArchiveError):
                plan_archive_range(
                    start, end, feeds=(DISPATCH_SCADA_FEED,), archived_dates=valid_dates
                )
        limits: tuple[Any, ...] = (True, None, -1)
        for limit in limits:
            with self.subTest(limit=limit), self.assertRaises(NemwebArchiveError):
                plan_archive_range(
                    datetime(2026, 8, 30, tzinfo=UTC),
                    datetime(2026, 8, 31, tzinfo=UTC),
                    feeds=(DISPATCH_SCADA_FEED,),
                    archived_dates=valid_dates,
                    max_artifacts=limit,
                )

    def test_rejects_malformed_availability_mappings(self) -> None:
        start = datetime(2026, 8, 30, tzinfo=UTC)
        end = datetime(2026, 8, 31, tzinfo=UTC)
        cases: tuple[Any, ...] = (
            [DISPATCH_SCADA_FEED],
            {DISPATCH_SCADA_FEED: "2026-08-30"},
            {DISPATCH_SCADA_FEED: (datetime(2026, 8, 30),)},
            {DISPATCH_SCADA_FEED: (None,)},
            {"unknown": (date(2026, 8, 30),)},
        )
        for archived_dates in cases:
            with self.subTest(archived_dates=archived_dates), self.assertRaises(
                NemwebArchiveError
            ):
                plan_archive_range(
                    start, end, feeds=(DISPATCH_SCADA_FEED,),
                    archived_dates=archived_dates,
                )


class OuterArchiveShapeTests(unittest.TestCase):
    def _item(self) -> ArchivePlanItem:
        return ArchivePlanItem(
            feed=DISPATCH_SCADA_FEED,
            report_date=_REPORT_DATE,
            source=ARCHIVE_SOURCE,
            url=_SCADA_URL,
        )

    def test_rejects_empty_or_non_bytes_outer_payload(self) -> None:
        payload: Any
        for payload in (b"", b"truncated", "not bytes", bytearray(b"PK")):
            with self.subTest(payload=type(payload).__name__), self.assertRaises(
                NemwebArchiveError
            ):
                extract_nested_archive(self._item(), payload, allow_reduced=True)

    def test_rejects_zero_member_outer_archive(self) -> None:
        with self.assertRaises(NemwebArchiveError):
            extract_nested_archive(
                self._item(), _outer_zip_members(()), allow_reduced=True
            )

    def test_accepts_stored_zip_members(self) -> None:
        csv_payload = _canonical_fixture_csv(
            "dispatch-scada-20260830-1455-reduced.csv",
            _SCADA_SOURCE_ID,
            "DISPATCHSCADA",
        )
        inner_buffer = BytesIO()
        with ZipFile(inner_buffer, "w", compression=ZIP_STORED) as archive:
            archive.writestr(_SCADA_MEMBER.removesuffix(".zip") + ".CSV", csv_payload)
        outer_payload = _outer_zip_members(
            ((_SCADA_MEMBER, inner_buffer.getvalue()),), compression=ZIP_STORED
        )

        extraction = extract_nested_archive(
            self._item(), outer_payload, allow_reduced=True
        )

        self.assertEqual(len(extraction.nested), 1)
        self.assertEqual(extraction.nested[0].member_name, _SCADA_MEMBER)
        self.assertEqual(
            extraction.nested[0].interval_timestamp,
            datetime(2026, 8, 30, 4, 55, tzinfo=UTC),
        )


def _tree(levels: tuple[tuple[str, bytes], ...]) -> bytes:
    return _outer_zip_members(levels)


class NestedArchiveTreeExtractionTests(unittest.TestCase):
    def _three_level_payloads(self) -> tuple[bytes, bytes, bytes, bytes]:
        leaf = b"nested leaf content"
        level3 = _tree((("leaf.txt", leaf),))
        level2 = _tree((("c.zip", level3),))
        level1 = _tree((("b.zip", level2),))
        root = _tree((("top.csv", b"top"), ("a.zip", level1)))
        return root, level1, level2, level3

    def test_extracts_multi_level_nested_archive_with_deterministic_paths(self) -> None:
        root, level1, level2, level3 = self._three_level_payloads()

        extraction = extract_nested_archive_tree(root)

        self.assertEqual(extraction.sha256, sha256(root).hexdigest())
        self.assertEqual(
            tuple(node.name for node in extraction.members), ("a.zip", "top.csv")
        )
        a_node = extraction.members[0]
        self.assertEqual((a_node.path, a_node.payload, a_node.sha256),
                         ("a.zip", level1, sha256(level1).hexdigest()))
        (b_node,) = a_node.children
        self.assertEqual((b_node.name, b_node.path, b_node.payload),
                         ("b.zip", "a.zip/b.zip", level2))
        (c_node,) = b_node.children
        self.assertEqual((c_node.name, c_node.path, c_node.payload),
                         ("c.zip", "a.zip/b.zip/c.zip", level3))
        (leaf_node,) = c_node.children
        self.assertEqual(
            (leaf_node.name, leaf_node.path, leaf_node.payload, leaf_node.children),
            ("leaf.txt", "a.zip/b.zip/c.zip/leaf.txt", b"nested leaf content", ()),
        )
        top_node = extraction.members[1]
        self.assertEqual((top_node.path, top_node.payload, top_node.children),
                         ("top.csv", b"top", ()))

    def test_tree_is_deterministic_independent_of_outer_member_order(self) -> None:
        root, level1, _, _ = self._three_level_payloads()
        reordered = _tree((("a.zip", level1), ("top.csv", b"top")))

        first = extract_nested_archive_tree(root)
        second = extract_nested_archive_tree(reordered)

        self.assertEqual(first.members, second.members)
        with self.assertRaises(FrozenInstanceError):
            first.members = ()  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            first.members[0].path = "changed"  # type: ignore[misc]

    def test_rejects_payloads_outside_supported_formats(self) -> None:
        valid_inner = _tree((("leaf.txt", b"leaf"),))
        cases: tuple[Any, ...] = (
            b"",
            b"truncated",
            "not bytes",
            bytearray(b"PK"),
            _outer_zip_members((("a.zip", b"x"),), compression=ZIP_BZIP2),
            _encrypted_outer_zip("a.zip", valid_inner),
            _outer_zip_members((("dir/", b""),)),
        )
        for payload in cases:
            with self.subTest(payload=type(payload).__name__), self.assertRaises(
                NemwebArchiveError
            ):
                extract_nested_archive_tree(payload)

    def test_rejects_unsafe_member_names(self) -> None:
        cases = (
            _outer_zip_members((("../evil.zip", b"x"),)),
            _outer_zip_members((("a\\b.zip", b"x"),)),
        )
        for payload in cases:
            with self.assertRaises(NemwebArchiveError):
                extract_nested_archive_tree(payload)
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr(ZipInfo(""), b"x")
        with self.assertRaises(NemwebArchiveError):
            extract_nested_archive_tree(buffer.getvalue())

    def test_rejects_nested_archives_beyond_depth_limit(self) -> None:
        level2 = _tree((("leaf.txt", b"leaf"),))
        level1 = _tree((("b.zip", level2),))
        two_level = _tree((("a.zip", level1),))

        self.assertEqual(
            extract_nested_archive_tree(two_level, max_depth=2).members[0].path,
            "a.zip",
        )
        with self.assertRaises(NemwebArchiveError):
            extract_nested_archive_tree(two_level, max_depth=1)
        with patch("batterywatch_api.nemweb_archives.MAX_NESTED_RECURSION_DEPTH", 1):
            with self.assertRaises(NemwebArchiveError):
                extract_nested_archive_tree(two_level)
            with self.assertRaises(NemwebArchiveError):
                extract_nested_archive_tree(two_level, max_depth=2)

    def test_rejects_invalid_depth_limits(self) -> None:
        payload = _tree((("leaf.txt", b"leaf"),))
        limits: tuple[Any, ...] = (0, -1, 4, True, "2")
        for limit in limits:
            with self.subTest(limit=limit), self.assertRaises(NemwebArchiveError):
                extract_nested_archive_tree(payload, max_depth=limit)

    def test_rejects_exactly_four_nested_levels_at_default_depth(self) -> None:
        self.assertEqual(MAX_NESTED_RECURSION_DEPTH, 3)
        four_level = _tree((
            ("a.zip", _tree((
                ("b.zip", _tree((
                    ("c.zip", _tree((
                        ("d.zip", _tree((("leaf.txt", b"leaf"),))),
                    ),)),
                ),)),
            ),)),
        ))
        with self.assertRaises(NemwebArchiveError):
            extract_nested_archive_tree(four_level)

    def test_rejects_duplicate_member_names_at_any_level(self) -> None:
        inner = _tree((("leaf.txt", b"leaf"),))
        cases = (
            _tree((("a.zip", inner), ("a.zip", inner))),
            _tree((("a.zip", _tree((("x.txt", b"1"), ("x.txt", b"2")))),)),
        )
        for payload in cases:
            with self.assertRaises(NemwebArchiveError):
                extract_nested_archive_tree(payload)

    def test_rejects_malformed_nested_zip_members(self) -> None:
        good_inner = _tree((("leaf.txt", b"leaf"),))
        truncated = good_inner[:-8]
        cases = (
            _tree((("a.zip", b"not a zip"),)),
            _tree((("a.zip", truncated),)),
            _tree((("a.zip", _tree((("empty.txt", b""),))),)),
        )
        for payload in cases:
            with self.assertRaises(NemwebArchiveError):
                extract_nested_archive_tree(payload)

    def test_rejects_empty_archives_at_any_level(self) -> None:
        with self.assertRaises(NemwebArchiveError):
            extract_nested_archive_tree(_tree(()))
        with self.assertRaises(NemwebArchiveError):
            extract_nested_archive_tree(_tree((("a.zip", _tree(())),)))

    def test_rejects_members_exceeding_size_and_count_bounds(self) -> None:
        filler = bytes(range(256)) * 40
        inner = _tree((("leaf.txt", filler),))
        payload = _tree((("a.zip", inner),))
        cases = (
            ("MAX_NESTED_ARCHIVE_BYTES", 1),
            ("MAX_OUTER_COMPRESSED_BYTES", 0),
            ("MAX_OUTER_UNCOMPRESSED_BYTES", 0),
            ("MAX_OUTER_ARCHIVE_BYTES", len(payload) - 1),
        )
        for constant, value in cases:
            with self.subTest(constant=constant), patch(
                f"batterywatch_api.nemweb_archives.{constant}", value
            ), self.assertRaises(NemwebArchiveError):
                extract_nested_archive_tree(payload)
        two_members = _tree((("a.txt", b"1"), ("b.txt", b"2")))
        with patch("batterywatch_api.nemweb_archives.MAX_DAILY_INTERVALS", 1):
            with self.assertRaises(NemwebArchiveError):
                extract_nested_archive_tree(two_members)

    def test_accepts_stored_compression_at_every_level(self) -> None:
        inner = _outer_zip_members((("leaf.csv", b"row"),), compression=ZIP_STORED)
        root = _outer_zip_members((("a.zip", inner),), compression=ZIP_STORED)

        extraction = extract_nested_archive_tree(root)

        (a_node,) = extraction.members
        (leaf_node,) = a_node.children
        self.assertEqual((leaf_node.path, leaf_node.payload), ("a.zip/leaf.csv", b"row"))


class PlannerExtractorIntegrationTests(unittest.TestCase):
    """A planned archive traversed through nested extraction, deterministically."""

    def _plan_single_day(self, feed: str) -> ArchivePlanItem:
        plan = plan_archive_range(
            datetime(2026, 8, 29, 14, 30, tzinfo=UTC),
            datetime(2026, 8, 30, 14, 0, tzinfo=UTC),
            feeds=(feed,),
            archived_dates={feed: (_REPORT_DATE,)},
        )
        (item,) = plan.items
        return item

    def test_planned_scada_archive_traverses_nested_extraction_deterministically(self) -> None:
        item = self._plan_single_day(DISPATCH_SCADA_FEED)
        self.assertEqual(
            (item.feed, item.report_date, item.source, item.url),
            (DISPATCH_SCADA_FEED, _REPORT_DATE, ARCHIVE_SOURCE, _SCADA_URL),
        )
        second_member = "PUBLIC_DISPATCHSCADA_202608301500_0000000535210765.zip"
        first_csv = _scada_csv_at("2026/08/30 14:55:00", _SCADA_SOURCE_ID)
        second_csv = _scada_csv_at("2026/08/30 15:00:00", "0000000535210765")
        outer_payload = _outer_zip_members((
            (second_member, _inner_zip(second_member, second_csv)),
            (_SCADA_MEMBER, _inner_zip(_SCADA_MEMBER, first_csv)),
        ))

        first = extract_nested_archive(item, outer_payload, allow_reduced=True)
        second = extract_nested_archive(item, outer_payload, allow_reduced=True)
        self.assertEqual(first, second)
        self.assertEqual(
            tuple(artifact.interval_timestamp for artifact in first.nested),
            (
                datetime(2026, 8, 30, 4, 55, tzinfo=UTC),
                datetime(2026, 8, 30, 5, 0, tzinfo=UTC),
            ),
        )

        tree = extract_nested_archive_tree(outer_payload)
        self.assertEqual(tree, extract_nested_archive_tree(outer_payload))
        self.assertEqual(tree.sha256, first.outer.sha256)
        self.assertEqual(
            tuple(node.name for node in tree.members),
            (_SCADA_MEMBER, second_member),
        )
        csv_by_member = {_SCADA_MEMBER: first_csv, second_member: second_csv}
        for node in tree.members:
            artifact = next(
                candidate for candidate in first.nested
                if candidate.member_name == node.name
            )
            self.assertEqual((node.payload, node.sha256),
                             (artifact.raw_bytes, artifact.sha256))
            (leaf,) = node.children
            self.assertEqual(
                (leaf.path, leaf.payload),
                (f"{node.name}/{node.name.removesuffix('.zip')}.CSV",
                 csv_by_member[node.name]),
            )

    def test_planned_price_archive_traverses_nested_tree_with_fixture_leaf(self) -> None:
        item = self._plan_single_day(DISPATCHIS_PRICE_FEED)
        self.assertEqual(
            (item.feed, item.report_date, item.source, item.url),
            (DISPATCHIS_PRICE_FEED, _REPORT_DATE, ARCHIVE_SOURCE, _PRICE_URL),
        )
        csv_payload = _canonical_fixture_csv(
            "dispatch-price-20260830-1500-reduced.csv",
            _PRICE_SOURCE_ID,
            "DISPATCHIS",
        )
        outer_payload = _outer_zip(_PRICE_MEMBER, _inner_zip(_PRICE_MEMBER, csv_payload))

        extraction = extract_nested_archive(item, outer_payload, allow_reduced=True)
        tree = extract_nested_archive_tree(outer_payload)

        self.assertEqual(extraction, extract_nested_archive(item, outer_payload, allow_reduced=True))
        self.assertEqual(tree, extract_nested_archive_tree(outer_payload))
        self.assertEqual(tree.sha256, extraction.outer.sha256)
        (node,) = tree.members
        self.assertEqual(node.name, _PRICE_MEMBER)
        self.assertEqual(node.payload, extraction.nested[0].raw_bytes)
        (leaf,) = node.children
        self.assertEqual(leaf.payload, csv_payload)
        self.assertEqual(
            parse_dispatch_price_mms_csv(
                leaf.payload.decode("utf-8"), source_id=_PRICE_SOURCE_ID,
                ingestion_version=0,
            )[0].region,
            "NSW1",
        )


if __name__ == "__main__":
    unittest.main()
