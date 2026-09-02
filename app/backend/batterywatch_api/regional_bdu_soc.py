"""Strict parser for regional aggregate BDU initial energy storage."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
from math import isfinite
import re
from typing import Literal

_NEM_TIMEZONE = timezone(timedelta(hours=10))
_METADATA_PREFIX = ("C", "NEMP.WORLD", "DISPATCHIS", "AEMO", "PUBLIC")
_TABLE_PREFIX = ("DISPATCH", "REGIONSUM", "9")
_REQUIRED_COLUMNS = frozenset(
    (
        "SETTLEMENTDATE",
        "RUNNO",
        "REGIONID",
        "DISPATCHINTERVAL",
        "INTERVENTION",
        "LASTCHANGED",
        "BDU_INITIAL_ENERGY_STORAGE",
    )
)
_REGIONS = frozenset(("NSW1", "QLD1", "SA1", "TAS1", "VIC1"))
_ARTIFACT_RE = re.compile(r"^[0-9a-f]{64}$")
_DISPATCH_INTERVAL_RE = re.compile(r"^(\d{8})(\d{3})$")


class RegionalBduSocParseError(ValueError):
    """Raised when regional aggregate BDU source data is unsafe to publish."""


def _timestamp(value: str, *, field: str, aligned: bool) -> datetime:
    normalized = value.strip().replace("/", "-").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RegionalBduSocParseError(f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_NEM_TIMEZONE)
    result = parsed.astimezone(timezone.utc)
    if aligned and (result.minute % 5 or result.second or result.microsecond):
        raise RegionalBduSocParseError(f"invalid {field} alignment")
    return result


def _report_timestamp(row: list[str]) -> datetime:
    if len(row) != 10 or tuple(row[:5]) != _METADATA_PREFIX:
        raise RegionalBduSocParseError("invalid DispatchIS metadata")
    return _timestamp(f"{row[5]} {row[6]}", field="report timestamp", aligned=False)


def _version(value: str, *, field: str, maximum: int) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise RegionalBduSocParseError(f"invalid {field}")
    parsed = int(value)
    if parsed > maximum:
        raise RegionalBduSocParseError(f"invalid {field}")
    return parsed


def _soc_mwh(value: str) -> float | None:
    if value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RegionalBduSocParseError("invalid BDU_INITIAL_ENERGY_STORAGE") from exc
    if not isfinite(parsed) or parsed < 0:
        raise RegionalBduSocParseError("invalid BDU_INITIAL_ENERGY_STORAGE")
    return parsed


def _dispatch_interval(value: str, interval_start: datetime) -> str:
    match = _DISPATCH_INTERVAL_RE.fullmatch(value)
    if match is None:
        raise RegionalBduSocParseError("invalid DISPATCHINTERVAL")
    period = int(match.group(2))
    if not 1 <= period <= 288:
        raise RegionalBduSocParseError("invalid DISPATCHINTERVAL")
    try:
        market_date = datetime.strptime(match.group(1), "%Y%m%d").replace(
            tzinfo=_NEM_TIMEZONE
        )
    except ValueError as exc:
        raise RegionalBduSocParseError("invalid DISPATCHINTERVAL") from exc
    expected = (market_date + timedelta(hours=4, minutes=period * 5)).astimezone(
        timezone.utc
    )
    if expected != interval_start:
        raise RegionalBduSocParseError(
            "DISPATCHINTERVAL does not match SETTLEMENTDATE"
        )
    return value


def _validated_downloaded_at(value: datetime, report_timestamp: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RegionalBduSocParseError("invalid downloaded_at")
    result = value.astimezone(timezone.utc)
    if result < report_timestamp:
        raise RegionalBduSocParseError("downloaded_at precedes report timestamp")
    return result


@dataclass(frozen=True, slots=True)
class RegionalBduSocObservation:
    """One region-level aggregate BDU initial-energy-storage observation."""

    region_id: str
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
    def scope(self) -> Literal["regional_aggregate"]:
        return "regional_aggregate"

    @property
    def publication_status(self) -> Literal["near_real_time_regional"]:
        return "near_real_time_regional"


def parse_dispatch_regionsum_bdu_soc(
    payload: str,
    *,
    source_artifact_id: str,
    downloaded_at: datetime,
    ingestion_version: int,
    correction_version: int = 0,
) -> tuple[RegionalBduSocObservation, ...]:
    """Parse public DispatchIS REGIONSUM v9 regional BDU storage rows."""

    if type(payload) is not str or not payload:
        raise RegionalBduSocParseError("invalid payload")
    if (
        type(source_artifact_id) is not str
        or _ARTIFACT_RE.fullmatch(source_artifact_id) is None
    ):
        raise RegionalBduSocParseError("invalid source artifact identity")
    ingestion = _version(
        str(ingestion_version), field="ingestion_version", maximum=2**63 - 1
    )
    correction = _version(
        str(correction_version), field="correction_version", maximum=2**63 - 1
    )

    reader = csv.reader(StringIO(payload))
    try:
        metadata = next(reader)
    except (StopIteration, csv.Error, TypeError, UnicodeError) as exc:
        raise RegionalBduSocParseError("incomplete DispatchIS report") from exc
    report_timestamp = _report_timestamp(metadata)
    downloaded = _validated_downloaded_at(downloaded_at, report_timestamp)

    header: list[str] | None = None
    observations: list[RegionalBduSocObservation] = []
    seen: set[tuple[str, datetime, int, int]] = set()
    report_record_count = 1
    expected_count: int | None = None
    try:
        for row in reader:
            report_record_count += 1
            if expected_count is not None:
                raise RegionalBduSocParseError("data follows report trailer")
            if row[:2] == ["C", "END OF REPORT"]:
                if len(row) != 3:
                    raise RegionalBduSocParseError("invalid report trailer")
                expected_count = _version(
                    row[2], field="report row count", maximum=10_000_000
                )
                continue

            if row[:4] == ["I", *_TABLE_PREFIX]:
                if header is not None or len(row) != len(set(row)):
                    raise RegionalBduSocParseError("invalid REGIONSUM header")
                if not _REQUIRED_COLUMNS.issubset(row[4:]):
                    raise RegionalBduSocParseError("missing REGIONSUM columns")
                header = row
                continue
            if row[:4] != ["D", *_TABLE_PREFIX]:
                continue
            if header is None or len(row) != len(header):
                raise RegionalBduSocParseError("malformed REGIONSUM row")
            values = dict(zip(header[4:], row[4:]))
            region_id = values["REGIONID"]
            if region_id not in _REGIONS:
                raise RegionalBduSocParseError("invalid REGIONID")
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
            if last_changed > report_timestamp:
                raise RegionalBduSocParseError("invalid source timestamp ordering")
            key = (region_id, interval_start, intervention, run_number)
            if key in seen:
                raise RegionalBduSocParseError("duplicate REGIONSUM observation")
            seen.add(key)
            observations.append(
                RegionalBduSocObservation(
                    region_id=region_id,
                    interval_start=interval_start,
                    soc_mwh=_soc_mwh(values["BDU_INITIAL_ENERGY_STORAGE"]),
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
        raise RegionalBduSocParseError("invalid CSV") from exc
    if expected_count is None:
        raise RegionalBduSocParseError("missing report trailer")
    if expected_count != report_record_count:
        raise RegionalBduSocParseError("report row count mismatch")
    if header is None:
        raise RegionalBduSocParseError("missing REGIONSUM v9 table")
    regional_groups: dict[tuple[datetime, int, int], set[str]] = {}
    for observation in observations:
        group_key = (
            observation.interval_start,
            observation.intervention,
            observation.run_number,
        )
        regional_groups.setdefault(group_key, set()).add(observation.region_id)
    if not regional_groups or any(
        regions != _REGIONS for regions in regional_groups.values()
    ):
        raise RegionalBduSocParseError("incomplete regional aggregate coverage")
    return tuple(
        sorted(
            observations,
            key=lambda item: (
                item.interval_start,
                item.region_id,
                item.intervention,
                item.run_number,
            ),
        )
    )


__all__ = [
    "RegionalBduSocObservation",
    "RegionalBduSocParseError",
    "parse_dispatch_regionsum_bdu_soc",
]
