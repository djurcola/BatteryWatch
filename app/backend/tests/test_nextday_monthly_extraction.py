"""Tests for bounded Next Day monthly archive extraction."""

from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
import struct
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from batterywatch_api.nextday_archives import (
    NEXTDAY_ARCHIVE_INDEX_URL,
    NextDayArchiveError,
    NextDayMonthlyArchiveRef,
)
from batterywatch_api.nextday_monthly_extraction import (
    read_nextday_daily_artifact,
    validate_nextday_monthly_archive,
)

UTC = timezone.utc
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "historical"
    / "nextday-unit-solution-soc-20260829-reduced.csv"
)


def _zip_bytes(filename: str, payload: bytes) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(filename, payload)
    return output.getvalue()


def _monthly_archive(
    *,
    day_numbers: tuple[int, ...] = tuple(range(1, 32)),
    first_outer_name: str | None = None,
    first_csv_name: str | None = None,
) -> tuple[NextDayMonthlyArchiveRef, bytes, bytes]:
    csv_payload = FIXTURE.read_bytes()
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as outer:
        for index, day in enumerate(day_numbers):
            publication_id = f"{47_000_000_000_000 + index + 1:016d}"
            stem = f"PUBLIC_NEXT_DAY_DISPATCH_202507{day:02d}_{publication_id}"
            outer_name = first_outer_name if index == 0 and first_outer_name else f"{stem}.zip"
            csv_name = first_csv_name if index == 0 and first_csv_name else f"{stem}.CSV"
            outer.writestr(
                outer_name,
                _zip_bytes(csv_name, csv_payload),
            )
    raw_bytes = output.getvalue()
    reference = NextDayMonthlyArchiveRef(
        date(2025, 7, 1),
        "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
        NEXTDAY_ARCHIVE_INDEX_URL + "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
        len(raw_bytes),
        datetime(2025, 9, 1, tzinfo=UTC),
    )
    return reference, raw_bytes, csv_payload


class NextDayMonthlyExtractionTests(unittest.TestCase):
    def test_validates_complete_month_then_reads_one_daily_artifact(self) -> None:
        reference, raw_bytes, csv_payload = _monthly_archive()

        manifest = validate_nextday_monthly_archive(reference, raw_bytes)
        daily = read_nextday_daily_artifact(manifest, raw_bytes, manifest.members[0])

        self.assertEqual(manifest.reference, reference)
        self.assertEqual(len(manifest.members), 31)
        self.assertEqual(
            (manifest.members[0].report_date, manifest.members[-1].report_date),
            (date(2025, 7, 1), date(2025, 7, 31)),
        )
        self.assertEqual(daily.member, manifest.members[0])
        self.assertEqual(daily.csv_bytes, csv_payload)
        self.assertEqual(
            daily.csv_filename,
            manifest.members[0].filename.removesuffix(".zip") + ".CSV",
        )
        self.assertEqual(len(daily.sha256), 64)
        self.assertEqual(daily.raw_zip_bytes[:2], b"PK")

    def test_rejects_incomplete_duplicate_and_unsafe_month_members(self) -> None:
        invalid_archives = (
            _monthly_archive(day_numbers=tuple(range(1, 31)))[:2],
            _monthly_archive(day_numbers=(1, 1, *range(3, 32)))[:2],
            _monthly_archive(first_outer_name="../unsafe.zip")[:2],
        )
        for reference, raw_bytes in invalid_archives:
            with self.subTest(size=len(raw_bytes)):
                with self.assertRaises(NextDayArchiveError):
                    validate_nextday_monthly_archive(reference, raw_bytes)

    def test_rejects_declared_oversized_daily_member(self) -> None:
        reference, raw_bytes, _ = _monthly_archive()
        corrupted = bytearray(raw_bytes)
        central = corrupted.find(b"PK\x01\x02")
        self.assertGreaterEqual(central, 0)
        struct.pack_into("<L", corrupted, central + 24, 16 * 1024 * 1024 + 1)
        changed = bytes(corrupted)
        changed_reference = NextDayMonthlyArchiveRef(
            reference.report_month,
            reference.filename,
            reference.url,
            len(changed),
            reference.listing_timestamp,
        )

        with self.assertRaises(NextDayArchiveError):
            validate_nextday_monthly_archive(changed_reference, changed)

    def test_rejects_wrong_inner_csv_and_changed_outer_bytes(self) -> None:
        reference, raw_bytes, _ = _monthly_archive(first_csv_name="wrong.CSV")
        manifest = validate_nextday_monthly_archive(reference, raw_bytes)
        with self.assertRaises(NextDayArchiveError):
            read_nextday_daily_artifact(manifest, raw_bytes, manifest.members[0])

        reference, raw_bytes, _ = _monthly_archive()
        manifest = validate_nextday_monthly_archive(reference, raw_bytes)
        changed = raw_bytes[:-1] + bytes((raw_bytes[-1] ^ 1,))
        with self.assertRaises(NextDayArchiveError):
            read_nextday_daily_artifact(manifest, changed, manifest.members[0])


if __name__ == "__main__":
    unittest.main()
