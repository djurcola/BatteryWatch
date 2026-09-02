"""Bounded planning and validation for historical NEMWeb daily archives."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import zlib
from typing import Final
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, LargeZipFile, ZipFile, ZipInfo

from .aemo import parse_dispatch_price_mms_csv
from .dispatch_scada import parse_dispatch_scada_csv
from .nemweb_dispatch_prices import (
    DispatchPriceArtifactRef as _PriceRef,
    extract_dispatch_price_zip as _extract_price_zip,
)
from .nemweb_dispatch_scada import (
    DispatchScadaArtifactRef as _ScadaRef,
    extract_dispatch_scada_zip as _extract_scada_zip,
)

NEMWEB_ORIGIN: Final = "https://www.nemweb.com.au"
DISPATCH_SCADA_FEED: Final = "dispatch_scada"
DISPATCHIS_PRICE_FEED: Final = "dispatchis_price"
ARCHIVE_SOURCE: Final = "archive"
CURRENT_INDEX_SOURCE: Final = "current_index"
DISPATCH_SCADA_CURRENT_INDEX_URL: Final = NEMWEB_ORIGIN + "/REPORTS/CURRENT/Dispatch_SCADA/"
DISPATCHIS_PRICE_CURRENT_INDEX_URL: Final = NEMWEB_ORIGIN + "/REPORTS/CURRENT/DispatchIS_Reports/"
DISPATCH_SCADA_ARCHIVE_BASE_URL: Final = NEMWEB_ORIGIN + "/REPORTS/ARCHIVE/Dispatch_SCADA/"
DISPATCHIS_PRICE_ARCHIVE_BASE_URL: Final = NEMWEB_ORIGIN + "/REPORTS/ARCHIVE/DispatchIS_Reports/"

MAX_ARCHIVE_RANGE_DAYS: Final = 366
MAX_ARCHIVE_ARTIFACTS: Final = (MAX_ARCHIVE_RANGE_DAYS + 1) * 2
MAX_OUTER_ARCHIVE_BYTES: Final = 128 * 1024 * 1024
MAX_OUTER_COMPRESSED_BYTES: Final = 128 * 1024 * 1024
MAX_OUTER_UNCOMPRESSED_BYTES: Final = 512 * 1024 * 1024
MAX_NESTED_ARCHIVE_BYTES: Final = 16 * 1024 * 1024
MAX_NESTED_COMPRESSION_RATIO: Final = 100
MAX_DAILY_INTERVALS: Final = 288

_UTC: Final = timezone.utc
_NEM_TIMEZONE: Final = timezone(timedelta(hours=10))
_FEEDS: Final = (DISPATCH_SCADA_FEED, DISPATCHIS_PRICE_FEED)
_FORMAT_ERRORS: Final = (BadZipFile, EOFError, KeyError, LargeZipFile,
                          NotImplementedError, OSError, OverflowError, RuntimeError,
                          UnicodeError, ValueError, zlib.error)


class NemwebArchiveError(ValueError):
    """Raised when a historical archive request or artifact is unsafe."""


@dataclass(frozen=True, slots=True)
class ArchivePlanItem:
    feed: str
    report_date: date
    source: str
    url: str


@dataclass(frozen=True, slots=True)
class ArchiveRangePlan:
    start: datetime
    end: datetime
    items: tuple[ArchivePlanItem, ...]


@dataclass(frozen=True, slots=True)
class NemwebOuterArchiveArtifact:
    feed: str
    report_date: date
    url: str
    filename: str
    sha256: str
    raw_bytes: bytes


@dataclass(frozen=True, slots=True)
class NemwebNestedArchiveArtifact:
    outer: NemwebOuterArchiveArtifact
    member_name: str
    source_artifact_id: str
    interval_timestamp: datetime
    sha256: str
    raw_bytes: bytes


@dataclass(frozen=True, slots=True)
class NemwebArchiveExtraction:
    outer: NemwebOuterArchiveArtifact
    nested: tuple[NemwebNestedArchiveArtifact, ...]


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise NemwebArchiveError(f"invalid archive {name}")
    try:
        if value.utcoffset() is None:
            raise NemwebArchiveError(f"invalid archive {name}")
        return value.astimezone(_UTC)
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise NemwebArchiveError(f"invalid archive {name}") from None


def _availability(values: Mapping[str, Iterable[date]] | None, name: str) -> dict[str, frozenset[date]]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise NemwebArchiveError(f"invalid {name}")
    result: dict[str, frozenset[date]] = {}
    for feed, entries in values.items():
        if type(feed) is not str or feed not in _FEEDS:
            raise NemwebArchiveError(f"unsupported archive feed in {name}")
        if isinstance(entries, (str, bytes)):
            raise NemwebArchiveError(f"invalid {name}")
        try:
            materialized = tuple(entries)
        except (TypeError, ValueError):
            raise NemwebArchiveError(f"invalid {name}") from None
        if any(type(entry) is not date for entry in materialized):
            raise NemwebArchiveError(f"unsupported archive date in {name}")
        result[feed] = frozenset(materialized)
    return result


def _report_dates(start: datetime, end: datetime) -> tuple[date, ...]:
    local_start_at = start.astimezone(_NEM_TIMEZONE)
    local_start = local_start_at.date()
    if local_start_at.timetz().replace(tzinfo=None) == time.min:
        local_start -= timedelta(days=1)
    local_end = end.astimezone(_NEM_TIMEZONE)
    last = local_end.date()
    if local_end.timetz().replace(tzinfo=None) == time.min:
        last -= timedelta(days=1)
    if last < local_start:
        return ()
    return tuple(local_start + timedelta(days=i) for i in range((last - local_start).days + 1))


def _archive_name(feed: str, report_date: date) -> str:
    prefix = "PUBLIC_DISPATCHSCADA_" if feed == DISPATCH_SCADA_FEED else "PUBLIC_DISPATCHIS_"
    stamp = f"{report_date.year:04d}{report_date.month:02d}{report_date.day:02d}"
    return f"{prefix}{stamp}.zip"


def _archive_source(feed: str, report_date: date, archived: Mapping[str, frozenset[date]], current: Mapping[str, frozenset[date]]) -> tuple[str, str]:
    if report_date in archived.get(feed, frozenset()):
        base = DISPATCH_SCADA_ARCHIVE_BASE_URL if feed == DISPATCH_SCADA_FEED else DISPATCHIS_PRICE_ARCHIVE_BASE_URL
        return ARCHIVE_SOURCE, f"{base}{_archive_name(feed, report_date)}"
    if report_date in current.get(feed, frozenset()):
        url = DISPATCH_SCADA_CURRENT_INDEX_URL if feed == DISPATCH_SCADA_FEED else DISPATCHIS_PRICE_CURRENT_INDEX_URL
        return CURRENT_INDEX_SOURCE, url
    raise NemwebArchiveError("unsupported or unavailable archive date")


def plan_archive_range(
    start: datetime,
    end: datetime,
    *,
    feeds: Iterable[str],
    archived_dates: Mapping[str, Iterable[date]] | None = None,
    current_index_dates: Mapping[str, Iterable[date]] | None = None,
    max_artifacts: int = MAX_ARCHIVE_ARTIFACTS,
) -> ArchiveRangePlan:
    """Create a deterministic bounded plan for an aware UTC half-open range."""
    start_utc, end_utc = _utc(start, "start"), _utc(end, "end")
    if end_utc <= start_utc:
        raise NemwebArchiveError("archive range must be increasing")
    if end_utc - start_utc > timedelta(days=MAX_ARCHIVE_RANGE_DAYS):
        raise NemwebArchiveError("archive range exceeds maximum")
    if type(max_artifacts) is not int or not 0 < max_artifacts <= MAX_ARCHIVE_ARTIFACTS:
        raise NemwebArchiveError("invalid archive artifact limit")
    try:
        requested = tuple(feeds)
    except (TypeError, ValueError):
        raise NemwebArchiveError("invalid archive feeds") from None
    if (not requested or any(type(feed) is not str or feed not in _FEEDS for feed in requested)
            or len(set(requested)) != len(requested)):
        raise NemwebArchiveError("invalid archive feeds")
    selected = tuple(feed for feed in _FEEDS if feed in requested)
    archived = _availability(archived_dates, "archived dates")
    current = _availability(current_index_dates, "current index dates")
    dates = _report_dates(start_utc, end_utc)
    if len(dates) > MAX_ARCHIVE_RANGE_DAYS:
        raise NemwebArchiveError("archive range exceeds maximum")
    if not dates or len(dates) * len(selected) > max_artifacts:
        raise NemwebArchiveError("archive plan exceeds artifact limit")
    items = tuple(
        ArchivePlanItem(feed, report_date, source, url)
        for report_date in dates
        for feed in selected
        for source, url in (_archive_source(feed, report_date, archived, current),)
    )
    return ArchiveRangePlan(start_utc, end_utc, items)


def _outer_identity(item: ArchivePlanItem, outer_url: str | None, outer_filename: str | None) -> tuple[str, str]:
    if (type(item) is not ArchivePlanItem or type(item.feed) is not str or item.feed not in _FEEDS
            or item.source != ARCHIVE_SOURCE or type(item.report_date) is not date
            or type(item.url) is not str):
        raise NemwebArchiveError("invalid daily archive plan item")
    filename = _archive_name(item.feed, item.report_date)
    base = DISPATCH_SCADA_ARCHIVE_BASE_URL if item.feed == DISPATCH_SCADA_FEED else DISPATCHIS_PRICE_ARCHIVE_BASE_URL
    expected_url = f"{base}{filename}"
    actual_url = item.url if outer_url is None else outer_url
    actual_filename = filename if outer_filename is None else outer_filename
    if (item.url != expected_url or type(actual_url) is not str
            or type(actual_filename) is not str
            or actual_url != expected_url or actual_filename != filename):
        raise NemwebArchiveError("invalid daily archive provenance")
    return filename, expected_url


def _nested_identity(feed: str, member_name: str, report_date: date) -> tuple[str, datetime]:
    prefix = "PUBLIC_DISPATCHSCADA_" if feed == DISPATCH_SCADA_FEED else "PUBLIC_DISPATCHIS_"
    if (type(member_name) is not str or not member_name.startswith(prefix)
            or not member_name.endswith(".zip") or "/" in member_name
            or "\\" in member_name or "\x00" in member_name):
        raise NemwebArchiveError("invalid nested archive member name")
    parts = member_name[len(prefix):-4].split("_")
    if len(parts) != 2 or len(parts[0]) != 12 or not parts[0].isascii() or not parts[0].isdecimal():
        raise NemwebArchiveError("invalid nested archive member name")
    source_id = parts[1]
    if not 1 <= len(source_id) <= 32 or not source_id.isascii() or not source_id.isdecimal():
        raise NemwebArchiveError("invalid nested archive member name")
    try:
        local = datetime.strptime(parts[0], "%Y%m%d%H%M").replace(tzinfo=_NEM_TIMEZONE)
    except (OverflowError, ValueError):
        raise NemwebArchiveError("invalid nested archive timestamp") from None
    local_time = local.timetz().replace(tzinfo=None)
    is_report_day_interval = local.date() == report_date and local_time != time.min
    is_following_midnight = (
        local.date() == report_date + timedelta(days=1)
        and local_time == time.min
    )
    if (not is_report_day_interval and not is_following_midnight) or local.minute % 5:
        raise NemwebArchiveError("nested archive timestamp is outside report date")
    return source_id, local.astimezone(_UTC)


def _member_sizes(member: ZipInfo) -> tuple[int, int]:
    if (type(member.filename) is not str or type(member.orig_filename) is not str
            or member.filename != member.orig_filename or member.is_dir()
            or member.flag_bits & 1 or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
            or type(member.file_size) is not int or type(member.compress_size) is not int
            or not 0 < member.file_size <= MAX_NESTED_ARCHIVE_BYTES
            or not 0 < member.compress_size <= MAX_NESTED_ARCHIVE_BYTES
            or member.file_size > member.compress_size * MAX_NESTED_COMPRESSION_RATIO):
        raise NemwebArchiveError("invalid nested archive member")
    return member.compress_size, member.file_size


def _read_nested_member(archive: ZipFile, member: ZipInfo) -> bytes:
    _member_sizes(member)
    try:
        with archive.open(member) as stream:
            payload = stream.read(MAX_NESTED_ARCHIVE_BYTES + 1)
            extra = stream.read(1)
    except _FORMAT_ERRORS as error:
        raise NemwebArchiveError("invalid nested archive member") from error
    if (type(payload) is not bytes or len(payload) > MAX_NESTED_ARCHIVE_BYTES
            or extra or len(payload) != member.file_size):
        raise NemwebArchiveError("invalid nested archive member")
    return payload


def _validate_inner(feed: str, member_name: str, source_id: str, timestamp: datetime, payload: bytes) -> None:
    try:
        if feed == DISPATCH_SCADA_FEED:
            reference = _ScadaRef(
                url=DISPATCH_SCADA_CURRENT_INDEX_URL + member_name,
                zip_filename=member_name, source_artifact_id=source_id,
                report_timestamp=timestamp,
            )
            artifact = _extract_scada_zip(reference, payload)
            records = parse_dispatch_scada_csv(
                artifact.csv_payload, source_artifact_id=source_id,
                ingestion_version=0, correction_version=0, naive_timezone=_NEM_TIMEZONE,
            )
        else:
            reference = _PriceRef(
                url=DISPATCHIS_PRICE_CURRENT_INDEX_URL + member_name,
                zip_filename=member_name, source_artifact_id=source_id,
                report_timestamp=timestamp,
            )
            artifact = _extract_price_zip(reference, payload)
            records = parse_dispatch_price_mms_csv(
                artifact.csv_payload, source_id=source_id,
                ingestion_version=0, correction_version=0,
            )
    except _FORMAT_ERRORS as error:
        raise NemwebArchiveError("invalid nested archive artifact") from error
    if not records or any(record.interval_start != timestamp for record in records):
        raise NemwebArchiveError("nested artifact timestamp does not match member")


def extract_nested_archive(
    item: ArchivePlanItem,
    outer_payload: bytes,
    *,
    outer_url: str | None = None,
    outer_filename: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    allow_reduced: bool = False,
) -> NemwebArchiveExtraction:
    """Validate a daily archive, including every nested interval ZIP."""
    if type(allow_reduced) is not bool:
        raise NemwebArchiveError("invalid reduced archive mode")
    if (start is None) != (end is None):
        raise NemwebArchiveError("archive filter requires both bounds")
    filter_start = filter_end = None
    if start is not None and end is not None:
        filter_start, filter_end = _utc(start, "filter start"), _utc(end, "filter end")
        if filter_end <= filter_start:
            raise NemwebArchiveError("archive filter range must be increasing")
    filename, url = _outer_identity(item, outer_url, outer_filename)
    if type(outer_payload) is not bytes or not outer_payload or len(outer_payload) > MAX_OUTER_ARCHIVE_BYTES:
        raise NemwebArchiveError("invalid outer archive payload")
    outer = NemwebOuterArchiveArtifact(item.feed, item.report_date, url, filename,
                                       sha256(outer_payload).hexdigest(), outer_payload)
    try:
        with ZipFile(BytesIO(outer_payload)) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_DAILY_INTERVALS:
                raise NemwebArchiveError("invalid daily archive member count")
            if len({member.filename for member in members}) != len(members):
                raise NemwebArchiveError("duplicate daily archive member")
            compressed = uncompressed = 0
            specs: list[tuple[ZipInfo, str, str, datetime]] = []
            source_ids: set[str] = set()
            timestamps: set[datetime] = set()
            for member in members:
                source_id, timestamp = _nested_identity(item.feed, member.filename, item.report_date)
                if source_id in source_ids or timestamp in timestamps:
                    raise NemwebArchiveError("duplicate nested archive identity")
                compressed_size, file_size = _member_sizes(member)
                source_ids.add(source_id)
                timestamps.add(timestamp)
                compressed += compressed_size
                uncompressed += file_size
                specs.append((member, member.filename, source_id, timestamp))
            if (compressed > MAX_OUTER_COMPRESSED_BYTES
                    or uncompressed > MAX_OUTER_UNCOMPRESSED_BYTES
                    or uncompressed > compressed * MAX_NESTED_COMPRESSION_RATIO):
                raise NemwebArchiveError("daily archive resource limits exceeded")
            if not allow_reduced:
                expected = tuple(
                    (datetime.combine(item.report_date, time.min, tzinfo=_NEM_TIMEZONE)
                     + timedelta(minutes=5 * (i + 1))).astimezone(_UTC)
                    for i in range(MAX_DAILY_INTERVALS)
                )
                if tuple(sorted(timestamps)) != expected:
                    raise NemwebArchiveError("daily archive is not a complete 288-interval report")
            nested: list[NemwebNestedArchiveArtifact] = []
            for member, member_name, source_id, timestamp in specs:
                payload = _read_nested_member(archive, member)
                _validate_inner(item.feed, member_name, source_id, timestamp, payload)
                nested.append(NemwebNestedArchiveArtifact(
                    outer, member_name, source_id, timestamp,
                    sha256(payload).hexdigest(), payload,
                ))
    except NemwebArchiveError:
        raise
    except _FORMAT_ERRORS as error:
        raise NemwebArchiveError("invalid outer archive") from error
    result = tuple(sorted(nested, key=lambda artifact: (
        artifact.interval_timestamp, artifact.source_artifact_id, artifact.member_name,
    )))
    if filter_start is not None and filter_end is not None:
        result = tuple(a for a in result if filter_start <= a.interval_timestamp < filter_end)
    return NemwebArchiveExtraction(outer, result)


__all__ = [
    "ARCHIVE_SOURCE", "ArchivePlanItem", "ArchiveRangePlan", "CURRENT_INDEX_SOURCE",
    "DISPATCHIS_PRICE_ARCHIVE_BASE_URL", "DISPATCHIS_PRICE_CURRENT_INDEX_URL",
    "DISPATCHIS_PRICE_FEED", "DISPATCH_SCADA_ARCHIVE_BASE_URL",
    "DISPATCH_SCADA_CURRENT_INDEX_URL", "DISPATCH_SCADA_FEED", "MAX_ARCHIVE_ARTIFACTS",
    "MAX_ARCHIVE_RANGE_DAYS", "MAX_DAILY_INTERVALS", "MAX_NESTED_ARCHIVE_BYTES",
    "MAX_NESTED_COMPRESSION_RATIO", "MAX_OUTER_ARCHIVE_BYTES",
    "MAX_OUTER_COMPRESSED_BYTES", "MAX_OUTER_UNCOMPRESSED_BYTES", "NemwebArchiveError",
    "NemwebArchiveExtraction", "NemwebNestedArchiveArtifact", "NemwebOuterArchiveArtifact",
    "NEMWEB_ORIGIN", "extract_nested_archive", "plan_archive_range",
]
