"""Bounded one-day-at-a-time extraction of Next Day monthly archives."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import BytesIO
import re
import zlib
from zipfile import (
    ZIP_DEFLATED,
    ZIP_STORED,
    BadZipFile,
    LargeZipFile,
    ZipFile,
    ZipInfo,
)

from .nextday_archives import (
    NEXTDAY_ARCHIVE_INDEX_URL,
    NEXTDAY_MONTHLY_ARCHIVE_MAX_BYTES,
    NextDayArchiveError,
    NextDayMonthlyArchiveRef,
)

MAX_NEXTDAY_DAILY_ZIP_BYTES = 16 * 1024 * 1024
MAX_NEXTDAY_DAILY_CSV_BYTES = 128 * 1024 * 1024
MAX_NEXTDAY_MONTH_EXPANDED_ZIP_BYTES = 512 * 1024 * 1024
MAX_NEXTDAY_COMPRESSION_RATIO = 100
_MEMBER_NAME_RE = re.compile(
    r"PUBLIC_NEXT_DAY_DISPATCH_([0-9]{4})([0-9]{2})([0-9]{2})_([0-9]{1,32})\.zip"
)
_FORMAT_ERRORS = (
    BadZipFile,
    EOFError,
    KeyError,
    LargeZipFile,
    NotImplementedError,
    OSError,
    OverflowError,
    RuntimeError,
    UnicodeError,
    ValueError,
    zlib.error,
)


@dataclass(frozen=True, slots=True)
class NextDayDailyMemberRef:
    report_date: date
    filename: str
    publication_id: str
    compressed_size: int
    expanded_size: int
    crc32: int


@dataclass(frozen=True, slots=True)
class NextDayMonthlyArchiveManifest:
    reference: NextDayMonthlyArchiveRef
    sha256: str
    members: tuple[NextDayDailyMemberRef, ...]


@dataclass(frozen=True, slots=True)
class NextDayDailyArtifact:
    member: NextDayDailyMemberRef
    sha256: str
    raw_zip_bytes: bytes
    csv_filename: str
    csv_bytes: bytes


def _reference_valid(reference: NextDayMonthlyArchiveRef, raw_bytes: bytes) -> bool:
    return (
        type(reference) is NextDayMonthlyArchiveRef
        and reference.report_month.day == 1
        and reference.filename
        == f"PUBLIC_NEXT_DAY_DISPATCH_{reference.report_month:%Y%m}01.zip"
        and reference.url == f"{NEXTDAY_ARCHIVE_INDEX_URL}{reference.filename}"
        and type(reference.size_bytes) is int
        and reference.size_bytes == len(raw_bytes)
        and 0 < len(raw_bytes) <= NEXTDAY_MONTHLY_ARCHIVE_MAX_BYTES
    )


def _safe_member_sizes(
    member: ZipInfo,
    *,
    max_compressed: int,
    max_expanded: int,
) -> tuple[int, int]:
    if (
        type(member.filename) is not str
        or type(member.orig_filename) is not str
        or member.filename != member.orig_filename
        or member.is_dir()
        or member.flag_bits & 1
        or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
        or member.comment
        or member.extra
        or type(member.compress_size) is not int
        or type(member.file_size) is not int
        or not 0 < member.compress_size <= max_compressed
        or not 0 < member.file_size <= max_expanded
        or member.file_size > member.compress_size * MAX_NEXTDAY_COMPRESSION_RATIO
    ):
        raise NextDayArchiveError("invalid Next Day archive member")
    return member.compress_size, member.file_size


def _member_identity(
    filename: str,
    report_month: date,
) -> tuple[date, str]:
    match = _MEMBER_NAME_RE.fullmatch(filename)
    if match is None or "/" in filename or "\\" in filename or "\x00" in filename:
        raise NextDayArchiveError("invalid Next Day daily member identity")
    try:
        report_date = date(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )
    except (OverflowError, ValueError):
        raise NextDayArchiveError("invalid Next Day daily member identity") from None
    if (report_date.year, report_date.month) != (
        report_month.year,
        report_month.month,
    ):
        raise NextDayArchiveError("Next Day daily member is outside report month")
    return report_date, match.group(4)


def validate_nextday_monthly_archive(
    reference: NextDayMonthlyArchiveRef,
    raw_bytes: bytes,
) -> NextDayMonthlyArchiveManifest:
    """Validate complete outer structure before any daily publication."""

    if type(raw_bytes) is not bytes or not _reference_valid(reference, raw_bytes):
        raise NextDayArchiveError("invalid Next Day monthly archive")
    try:
        with ZipFile(BytesIO(raw_bytes)) as archive:
            if archive.comment:
                raise NextDayArchiveError("invalid Next Day monthly archive")
            infos = archive.infolist()
            expected_count = monthrange(
                reference.report_month.year,
                reference.report_month.month,
            )[1]
            if len(infos) != expected_count:
                raise NextDayArchiveError("incomplete Next Day monthly archive")
            if len({item.filename for item in infos}) != len(infos):
                raise NextDayArchiveError("duplicate Next Day daily member")
            members: list[NextDayDailyMemberRef] = []
            total_expanded = 0
            for info in infos:
                compressed, expanded = _safe_member_sizes(
                    info,
                    max_compressed=MAX_NEXTDAY_DAILY_ZIP_BYTES,
                    max_expanded=MAX_NEXTDAY_DAILY_ZIP_BYTES,
                )
                report_date, publication_id = _member_identity(
                    info.filename,
                    reference.report_month,
                )
                total_expanded += expanded
                if total_expanded > MAX_NEXTDAY_MONTH_EXPANDED_ZIP_BYTES:
                    raise NextDayArchiveError("Next Day monthly archive expands too large")
                members.append(
                    NextDayDailyMemberRef(
                        report_date,
                        info.filename,
                        publication_id,
                        compressed,
                        expanded,
                        info.CRC,
                    )
                )
            members.sort(key=lambda item: item.report_date)
            expected_dates = tuple(
                date(reference.report_month.year, reference.report_month.month, day)
                for day in range(1, expected_count + 1)
            )
            if tuple(item.report_date for item in members) != expected_dates:
                raise NextDayArchiveError("incomplete Next Day monthly archive")
    except NextDayArchiveError:
        raise
    except _FORMAT_ERRORS as error:
        raise NextDayArchiveError("invalid Next Day monthly archive") from error
    return NextDayMonthlyArchiveManifest(
        reference,
        sha256(raw_bytes).hexdigest(),
        tuple(members),
    )


def _member_info_matches(info: ZipInfo, member: NextDayDailyMemberRef) -> bool:
    return (
        info.filename == member.filename
        and info.compress_size == member.compressed_size
        and info.file_size == member.expanded_size
        and info.CRC == member.crc32
    )


def read_nextday_daily_artifact(
    manifest: NextDayMonthlyArchiveManifest,
    raw_bytes: bytes,
    member: NextDayDailyMemberRef,
) -> NextDayDailyArtifact:
    """Read and verify exactly one selected daily ZIP and its single CSV."""

    if (
        type(manifest) is not NextDayMonthlyArchiveManifest
        or type(raw_bytes) is not bytes
        or type(member) is not NextDayDailyMemberRef
        or len(raw_bytes) != manifest.reference.size_bytes
        or sha256(raw_bytes).hexdigest() != manifest.sha256
        or member not in manifest.members
    ):
        raise NextDayArchiveError("invalid Next Day daily artifact request")
    try:
        with ZipFile(BytesIO(raw_bytes)) as outer:
            info = outer.getinfo(member.filename)
            if not _member_info_matches(info, member):
                raise NextDayArchiveError("changed Next Day daily member")
            with outer.open(info) as stream:
                daily_zip = stream.read(MAX_NEXTDAY_DAILY_ZIP_BYTES + 1)
            if type(daily_zip) is not bytes or len(daily_zip) != member.expanded_size:
                raise NextDayArchiveError("invalid Next Day daily member")

        with ZipFile(BytesIO(daily_zip)) as daily:
            if daily.comment:
                raise NextDayArchiveError("invalid Next Day daily artifact")
            csv_infos = daily.infolist()
            if len(csv_infos) != 1:
                raise NextDayArchiveError("invalid Next Day daily artifact")
            csv_info = csv_infos[0]
            _safe_member_sizes(
                csv_info,
                max_compressed=MAX_NEXTDAY_DAILY_ZIP_BYTES,
                max_expanded=MAX_NEXTDAY_DAILY_CSV_BYTES,
            )
            expected_csv = member.filename.removesuffix(".zip") + ".CSV"
            if csv_info.filename != expected_csv:
                raise NextDayArchiveError("invalid Next Day daily CSV identity")
            with daily.open(csv_info) as stream:
                csv_bytes = stream.read(MAX_NEXTDAY_DAILY_CSV_BYTES + 1)
            if type(csv_bytes) is not bytes or len(csv_bytes) != csv_info.file_size:
                raise NextDayArchiveError("invalid Next Day daily CSV")
    except NextDayArchiveError:
        raise
    except _FORMAT_ERRORS as error:
        raise NextDayArchiveError("invalid Next Day daily artifact") from error
    return NextDayDailyArtifact(
        member,
        sha256(daily_zip).hexdigest(),
        daily_zip,
        expected_csv,
        csv_bytes,
    )


__all__ = [
    "MAX_NEXTDAY_COMPRESSION_RATIO",
    "MAX_NEXTDAY_DAILY_CSV_BYTES",
    "MAX_NEXTDAY_DAILY_ZIP_BYTES",
    "MAX_NEXTDAY_MONTH_EXPANDED_ZIP_BYTES",
    "NextDayDailyArtifact",
    "NextDayDailyMemberRef",
    "NextDayMonthlyArchiveManifest",
    "read_nextday_daily_artifact",
    "validate_nextday_monthly_archive",
]
