"""Bounded discovery and planning for Next Day Dispatch monthly archives."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import re
from typing import Final

NEXTDAY_ARCHIVE_INDEX_URL: Final = (
    "https://www.nemweb.com.au/REPORTS/ARCHIVE/Next_Day_Dispatch/"
)
NEXTDAY_MONTHLY_ARCHIVE_MAX_BYTES: Final = 256 * 1024 * 1024
NEXTDAY_ARCHIVE_INDEX_MAX_BYTES: Final = 64 * 1024
MAX_NEXTDAY_ARCHIVE_RANGE_DAYS: Final = 366
MAX_NEXTDAY_ARCHIVE_MONTHS: Final = 13
_NEM_TIMEZONE: Final = timezone(timedelta(hours=10))
_MONTHLY_NAME_RE: Final = re.compile(
    r"PUBLIC_NEXT_DAY_DISPATCH_([0-9]{4})([0-9]{2})01\.zip"
)
_LISTING_PREFIX_RE: Final = re.compile(
    r"\s*([A-Z][a-z]+, [A-Z][a-z]+ [0-9]{1,2}, [0-9]{4} "
    r"[0-9]{2}:[0-9]{2} [AP]M)\s+([0-9]+)\s*$"
)


class NextDayArchiveError(ValueError):
    """Raised when the monthly archive listing or request is invalid."""


@dataclass(frozen=True, slots=True)
class NextDayMonthlyArchiveRef:
    report_month: date
    filename: str
    url: str
    size_bytes: int
    listing_timestamp: datetime


@dataclass(frozen=True, slots=True)
class NextDayMonthlyArchivePlan:
    start: datetime
    end: datetime
    items: tuple[NextDayMonthlyArchiveRef, ...]
    missing_months: tuple[date, ...]


class _ListingCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._text: list[str] = []
        self._href: str | None = None
        self._prefix = ""
        self._anchor_text: list[str] = []
        self.links: list[tuple[str, str, str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.lower()
        if lowered == "br":
            self._text.clear()
            return
        if lowered != "a" or self._href is not None:
            return
        hrefs = [value for name, value in attrs if name.lower() == "href"]
        self._href = hrefs[0] if len(hrefs) == 1 else ""
        self._prefix = "".join(self._text)
        self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._href is None:
            self._text.append(data)
        else:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        self.links.append(
            (self._href, "".join(self._anchor_text), self._prefix)
        )
        self._href = None
        self._prefix = ""
        self._anchor_text = []
        self._text.clear()


def _listing_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%A, %B %d, %Y %I:%M %p")
    except (OverflowError, ValueError):
        raise NextDayArchiveError("invalid Next Day archive listing") from None
    weekday = value.split(",", 1)[0]
    if parsed.strftime("%A") != weekday:
        raise NextDayArchiveError("invalid Next Day archive listing")
    return parsed.replace(tzinfo=_NEM_TIMEZONE).astimezone(timezone.utc)


def discover_nextday_monthly_archives(
    index_html: str,
    *,
    index_url: str,
) -> tuple[NextDayMonthlyArchiveRef, ...]:
    """Return canonical available monthly references in month order."""

    if index_url != NEXTDAY_ARCHIVE_INDEX_URL or type(index_html) is not str:
        raise NextDayArchiveError("invalid Next Day archive listing")
    try:
        encoded_size = len(index_html.encode("utf-8"))
    except UnicodeEncodeError:
        raise NextDayArchiveError("invalid Next Day archive listing") from None
    if not 0 < encoded_size <= NEXTDAY_ARCHIVE_INDEX_MAX_BYTES:
        raise NextDayArchiveError("invalid Next Day archive listing")

    collector = _ListingCollector()
    try:
        collector.feed(index_html)
        collector.close()
    except (TypeError, ValueError):
        raise NextDayArchiveError("invalid Next Day archive listing") from None

    references: dict[date, NextDayMonthlyArchiveRef] = {}
    for href, anchor_text, prefix in collector.links:
        if "PUBLIC_NEXT_DAY_DISPATCH_" not in href and (
            "PUBLIC_NEXT_DAY_DISPATCH_" not in anchor_text
        ):
            continue
        filename_match = _MONTHLY_NAME_RE.fullmatch(anchor_text)
        if filename_match is None:
            raise NextDayArchiveError("invalid Next Day archive listing")
        filename = anchor_text
        expected_href = f"/Reports/ARCHIVE/Next_Day_Dispatch/{filename}"
        if href != expected_href:
            raise NextDayArchiveError("invalid Next Day archive listing")
        prefix_match = _LISTING_PREFIX_RE.fullmatch(prefix)
        if prefix_match is None:
            raise NextDayArchiveError("invalid Next Day archive listing")
        try:
            report_month = date(
                int(filename_match.group(1)),
                int(filename_match.group(2)),
                1,
            )
            size_bytes = int(prefix_match.group(2))
        except (OverflowError, ValueError):
            raise NextDayArchiveError("invalid Next Day archive listing") from None
        if not 0 < size_bytes <= NEXTDAY_MONTHLY_ARCHIVE_MAX_BYTES:
            raise NextDayArchiveError("invalid Next Day archive listing")
        reference = NextDayMonthlyArchiveRef(
            report_month,
            filename,
            f"{NEXTDAY_ARCHIVE_INDEX_URL}{filename}",
            size_bytes,
            _listing_timestamp(prefix_match.group(1)),
        )
        existing = references.get(report_month)
        if existing is not None and existing != reference:
            raise NextDayArchiveError("conflicting Next Day archive listing")
        if existing is not None:
            raise NextDayArchiveError("duplicate Next Day archive listing")
        references[report_month] = reference

    if not references:
        raise NextDayArchiveError("empty Next Day archive listing")
    return tuple(references[month] for month in sorted(references))


def _strict_utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise NextDayArchiveError(f"invalid Next Day archive {name}")
    try:
        offset = value.utcoffset()
    except (OverflowError, TypeError, ValueError):
        raise NextDayArchiveError(f"invalid Next Day archive {name}") from None
    if offset != timedelta(0):
        raise NextDayArchiveError(f"Next Day archive {name} must use UTC")
    return value.astimezone(timezone.utc)


def _next_month(value: date) -> date:
    return (
        date(value.year + 1, 1, 1)
        if value.month == 12
        else date(value.year, value.month + 1, 1)
    )


def _report_date_for_interval(value: datetime) -> date:
    local = value.astimezone(_NEM_TIMEZONE)
    return (
        local.date() - timedelta(days=1)
        if (local.hour, local.minute) <= (4, 0)
        else local.date()
    )


def nextday_report_date_bounds(
    start: datetime,
    end: datetime,
) -> tuple[date, date] | None:
    """Return report-date bounds containing aligned five-minute intervals."""

    start_utc = _strict_utc(start, "start")
    end_utc = _strict_utc(end, "end")
    if end_utc <= start_utc:
        raise NextDayArchiveError("Next Day archive range must be increasing")
    first_interval = start_utc.replace(
        minute=start_utc.minute - (start_utc.minute % 5),
        second=0,
        microsecond=0,
    )
    if first_interval < start_utc:
        first_interval += timedelta(minutes=5)
    end_probe = end_utc - timedelta(microseconds=1)
    last_interval = end_probe.replace(
        minute=end_probe.minute - (end_probe.minute % 5),
        second=0,
        microsecond=0,
    )
    if first_interval > last_interval:
        return None
    return (
        _report_date_for_interval(first_interval),
        _report_date_for_interval(last_interval),
    )


def _intersecting_months(start: datetime, end: datetime) -> tuple[date, ...]:
    bounds = nextday_report_date_bounds(start, end)
    if bounds is None:
        return ()
    first_report, last_report = bounds
    current = date(first_report.year, first_report.month, 1)
    last = date(last_report.year, last_report.month, 1)
    months: list[date] = []
    while current <= last:
        months.append(current)
        current = _next_month(current)
    return tuple(months)


def plan_nextday_monthly_archives(
    start: datetime,
    end: datetime,
    references: Iterable[NextDayMonthlyArchiveRef],
    *,
    max_months: int = MAX_NEXTDAY_ARCHIVE_MONTHS,
) -> NextDayMonthlyArchivePlan:
    """Plan bounded available monthly artifacts for a strict UTC range."""

    start_utc = _strict_utc(start, "start")
    end_utc = _strict_utc(end, "end")
    if end_utc <= start_utc:
        raise NextDayArchiveError("Next Day archive range must be increasing")
    if end_utc - start_utc > timedelta(days=MAX_NEXTDAY_ARCHIVE_RANGE_DAYS):
        raise NextDayArchiveError("Next Day archive range exceeds maximum")
    if type(max_months) is not int or not 0 < max_months <= MAX_NEXTDAY_ARCHIVE_MONTHS:
        raise NextDayArchiveError("invalid Next Day archive month limit")
    try:
        available = tuple(references)
    except (TypeError, ValueError):
        raise NextDayArchiveError("invalid Next Day archive references") from None
    if any(type(item) is not NextDayMonthlyArchiveRef for item in available):
        raise NextDayArchiveError("invalid Next Day archive references")
    by_month: dict[date, NextDayMonthlyArchiveRef] = {}
    for item in available:
        if (
            item.report_month.day != 1
            or item.filename != f"PUBLIC_NEXT_DAY_DISPATCH_{item.report_month:%Y%m}01.zip"
            or item.url != f"{NEXTDAY_ARCHIVE_INDEX_URL}{item.filename}"
            or not 0 < item.size_bytes <= NEXTDAY_MONTHLY_ARCHIVE_MAX_BYTES
            or item.listing_timestamp.tzinfo is None
            or item.listing_timestamp.utcoffset() != timedelta(0)
            or item.report_month in by_month
        ):
            raise NextDayArchiveError("invalid Next Day archive references")
        by_month[item.report_month] = item
    months = _intersecting_months(start_utc, end_utc)
    if not months or len(months) > max_months:
        raise NextDayArchiveError("Next Day archive plan exceeds month limit")
    return NextDayMonthlyArchivePlan(
        start_utc,
        end_utc,
        tuple(by_month[month] for month in months if month in by_month),
        tuple(month for month in months if month not in by_month),
    )


__all__ = [
    "MAX_NEXTDAY_ARCHIVE_MONTHS",
    "MAX_NEXTDAY_ARCHIVE_RANGE_DAYS",
    "NEXTDAY_ARCHIVE_INDEX_MAX_BYTES",
    "NEXTDAY_ARCHIVE_INDEX_URL",
    "NEXTDAY_MONTHLY_ARCHIVE_MAX_BYTES",
    "NextDayArchiveError",
    "NextDayMonthlyArchivePlan",
    "NextDayMonthlyArchiveRef",
    "discover_nextday_monthly_archives",
    "nextday_report_date_bounds",
    "plan_nextday_monthly_archives",
]
