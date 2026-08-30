"""Tests for the bounded NEMWeb historical archive seam."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZipFile, ZipInfo

from batterywatch_api.aemo import parse_dispatch_price_mms_csv
from batterywatch_api.nemweb_archives import (
    ARCHIVE_SOURCE,
    CURRENT_INDEX_SOURCE,
    DISPATCHIS_PRICE_FEED,
    DISPATCH_SCADA_FEED,
    ArchivePlanItem,
    NemwebArchiveError,
    extract_nested_archive,
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
        info = ZipInfo(member_name)
        info.flag_bits |= 1
        archive.writestr(info, inner_payload)
    return buffer.getvalue()


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


if __name__ == "__main__":
    unittest.main()
