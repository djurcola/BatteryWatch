"""Strict parser for authoritative Next Day Dispatch UnitSolution SOC."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
from math import isfinite
import re
from typing import Literal

_NEM_TIMEZONE = timezone(timedelta(hours=10))
_METADATA_PREFIX = ("C", "NEMP.WORLD", "NEXT_DAY_DISPATCH", "AEMO", "PUBLIC")
_TABLE_PREFIX = ("DISPATCH", "UNIT_SOLUTION")
_ACCEPTED_TABLE_VERSIONS = frozenset(("5", "6"))
_REQUIRED_COLUMNS = frozenset(
    (
        "SETTLEMENTDATE",
        "RUNNO",
        "DUID",
        "DISPATCHINTERVAL",
        "INTERVENTION",
        "LASTCHANGED",
        "INITIAL_ENERGY_STORAGE",
    )
)
_DUID_RE = re.compile(r"^[A-Z0-9_]{1,32}$")
_ARTIFACT_RE = re.compile(r"^[0-9a-f]{64}$")
_DISPATCH_INTERVAL_RE = re.compile(r"^(\d{8})(\d{3})$")


class NextDaySocParseError(ValueError):
    """Raised when Next Day SOC source data cannot be normalized safely."""


def _timestamp(value: str, *, field: str, aligned: bool) -> datetime:
    normalized = value.strip().replace("/", "-").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise NextDaySocParseError(f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_NEM_TIMEZONE)
    result = parsed.astimezone(timezone.utc)
    if aligned and (result.minute % 5 or result.second or result.microsecond):
        raise NextDaySocParseError(f"invalid {field} alignment")
    return result


def _report_timestamp(row: list[str]) -> datetime:
    if len(row) != 10 or tuple(row[:5]) != _METADATA_PREFIX:
        raise NextDaySocParseError("invalid Next Day metadata")
    return _timestamp(f"{row[5]} {row[6]}", field="report timestamp", aligned=False)


def _version(value: str, *, field: str, maximum: int) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise NextDaySocParseError(f"invalid {field}")
    parsed = int(value)
    if parsed > maximum:
        raise NextDaySocParseError(f"invalid {field}")
    return parsed


def _soc_mwh(value: str) -> float | None:
    if value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise NextDaySocParseError("invalid INITIAL_ENERGY_STORAGE") from exc
    if not isfinite(parsed) or parsed < 0:
        raise NextDaySocParseError("invalid INITIAL_ENERGY_STORAGE")
    return parsed


def _dispatch_interval(value: str, interval_start: datetime) -> str:
    match = _DISPATCH_INTERVAL_RE.fullmatch(value)
    if match is None:
        raise NextDaySocParseError("invalid DISPATCHINTERVAL")
    period = int(match.group(2))
    if not 1 <= period <= 288:
        raise NextDaySocParseError("invalid DISPATCHINTERVAL")
    try:
        market_date = datetime.strptime(match.group(1), "%Y%m%d").replace(
            tzinfo=_NEM_TIMEZONE
        )
    except ValueError as exc:
        raise NextDaySocParseError("invalid DISPATCHINTERVAL") from exc
    expected = (market_date + timedelta(hours=4, minutes=period * 5)).astimezone(
        timezone.utc
    )
    if expected != interval_start:
        raise NextDaySocParseError("DISPATCHINTERVAL does not match SETTLEMENTDATE")
    return value


def _validated_downloaded_at(value: datetime, report_timestamp: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise NextDaySocParseError("invalid downloaded_at")
    result = value.astimezone(timezone.utc)
    if result < report_timestamp:
        raise NextDaySocParseError("downloaded_at precedes report timestamp")
    return result


@dataclass(frozen=True, slots=True)
class NextDaySocObservation:
    """One authoritative per-DUID initial energy storage observation."""

    duid: str
    interval_start: datetime
    soc_mwh: float | None
    intervention: int
    run_number: int
    dispatch_interval: str
    last_changed: datetime
    source_artifact_id: str
    report_timestamp: datetime
    downloaded_at: datetime
    ingestion_version: int
    correction_version: int

    @property
    def publication_latency_seconds(self) -> int:
        return int((self.report_timestamp - self.interval_start).total_seconds())

    @property
    def publication_status(self) -> Literal["next_day"]:
        return "next_day"


def parse_nextday_unit_solution_soc(
    payload: str,
    *,
    duids: frozenset[str],
    source_artifact_id: str,
    downloaded_at: datetime,
    ingestion_version: int,
    correction_version: int = 0,
) -> tuple[NextDaySocObservation, ...]:
    """Parse public Next Day UnitSolution v5/v6 rows for reviewed DUIDs."""

    if type(payload) is not str or not payload:
        raise NextDaySocParseError("invalid payload")
    if type(duids) is not frozenset or not duids or any(
        type(duid) is not str or _DUID_RE.fullmatch(duid) is None for duid in duids
    ):
        raise NextDaySocParseError("invalid reviewed DUID set")
    if type(source_artifact_id) is not str or _ARTIFACT_RE.fullmatch(source_artifact_id) is None:
        raise NextDaySocParseError("invalid source artifact identity")
    ingestion = _version(str(ingestion_version), field="ingestion_version", maximum=2**63 - 1)
    correction = _version(str(correction_version), field="correction_version", maximum=2**63 - 1)

    reader = csv.reader(StringIO(payload))
    try:
        metadata = next(reader)
    except (StopIteration, csv.Error, TypeError, UnicodeError) as exc:
        raise NextDaySocParseError("incomplete Next Day report") from exc
    report_timestamp = _report_timestamp(metadata)
    downloaded = _validated_downloaded_at(downloaded_at, report_timestamp)

    header: list[str] | None = None
    table_identity: tuple[str, ...] | None = None
    observations: list[NextDaySocObservation] = []
    seen: set[tuple[str, datetime, int, int]] = set()
    report_record_count = 1
    expected_count: int | None = None
    try:
        for row in reader:
            report_record_count += 1
            if expected_count is not None:
                raise NextDaySocParseError("data follows report trailer")
            if row[:2] == ["C", "END OF REPORT"]:
                if len(row) != 3:
                    raise NextDaySocParseError("invalid report trailer")
                expected_count = _version(
                    row[2], field="report row count", maximum=10_000_000
                )
                continue

            if row[:3] == ["I", *_TABLE_PREFIX]:
                if len(row) < 4 or row[3] not in _ACCEPTED_TABLE_VERSIONS:
                    raise NextDaySocParseError("unsupported UnitSolution version")
                if header is not None or len(row) != len(set(row)):
                    raise NextDaySocParseError("invalid UnitSolution header")
                if not _REQUIRED_COLUMNS.issubset(row[4:]):
                    raise NextDaySocParseError("missing UnitSolution columns")
                header = row
                table_identity = tuple(row[1:4])
                continue
            if row[:3] != ["D", *_TABLE_PREFIX]:
                continue
            if (
                header is None
                or table_identity is None
                or tuple(row[1:4]) != table_identity
                or len(row) != len(header)
            ):
                raise NextDaySocParseError("malformed UnitSolution row")
            values = dict(zip(header[4:], row[4:]))
            duid = values["DUID"]
            if duid not in duids:
                continue
            interval_start = _timestamp(
                values["SETTLEMENTDATE"], field="SETTLEMENTDATE", aligned=True
            )
            run_number = _version(values["RUNNO"], field="RUNNO", maximum=999)
            intervention = _version(
                values["INTERVENTION"], field="INTERVENTION", maximum=1
            )
            dispatch_interval = _dispatch_interval(
                values["DISPATCHINTERVAL"], interval_start
            )
            last_changed = _timestamp(
                values["LASTCHANGED"], field="LASTCHANGED", aligned=False
            )
            if last_changed > report_timestamp or report_timestamp <= interval_start:
                raise NextDaySocParseError("invalid source timestamp ordering")
            key = (duid, interval_start, intervention, run_number)
            if key in seen:
                raise NextDaySocParseError("duplicate UnitSolution observation")
            seen.add(key)
            observations.append(
                NextDaySocObservation(
                    duid=duid,
                    interval_start=interval_start,
                    soc_mwh=_soc_mwh(values["INITIAL_ENERGY_STORAGE"]),
                    intervention=intervention,
                    run_number=run_number,
                    dispatch_interval=dispatch_interval,
                    last_changed=last_changed,
                    source_artifact_id=source_artifact_id,
                    report_timestamp=report_timestamp,
                    downloaded_at=downloaded,
                    ingestion_version=ingestion,
                    correction_version=correction,
                )
            )
    except (csv.Error, TypeError, UnicodeError) as exc:
        raise NextDaySocParseError("invalid CSV") from exc
    if expected_count is None:
        raise NextDaySocParseError("missing report trailer")
    if expected_count != report_record_count:
        raise NextDaySocParseError("report row count mismatch")
    if header is None:
        raise NextDaySocParseError("missing UnitSolution v5/v6 table")
    return tuple(
        sorted(
            observations,
            key=lambda item: (
                item.duid,
                item.interval_start,
                item.intervention,
                item.run_number,
            ),
        )
    )


__all__ = [
    "NextDaySocObservation",
    "NextDaySocParseError",
    "parse_nextday_unit_solution_soc",
]
