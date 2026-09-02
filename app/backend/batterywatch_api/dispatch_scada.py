"""Parser for a canonical AEMO Dispatch SCADA artifact."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone, tzinfo
from io import StringIO

from .storage import GeneratorPower5m


class DispatchScadaParseError(ValueError):
    """Raised when a Dispatch SCADA CSV member is not canonical."""


__all__ = ["DispatchScadaParseError", "parse_dispatch_scada_csv"]

_METADATA_PREFIX = ("C", "NEMP.WORLD", "DISPATCHSCADA", "AEMO", "PUBLIC")
_HEADER = (
    "I",
    "DISPATCH",
    "UNIT_SCADA",
    "1",
    "SETTLEMENTDATE",
    "DUID",
    "SCADAVALUE",
    "LASTCHANGED",
)
_DATA_PREFIX = ("D", "DISPATCH", "UNIT_SCADA", "1")
_FOOTER_PREFIX = ("C", "END OF REPORT")


def _read_rows(payload: str) -> list[list[str]]:
    if not isinstance(payload, str):
        raise DispatchScadaParseError("row 1: payload must be CSV text")
    try:
        reader = csv.reader(StringIO(payload), strict=True)
    except (TypeError, ValueError, UnicodeError):
        raise DispatchScadaParseError("row 1: payload must be CSV text") from None
    try:
        return list(reader)
    except (csv.Error, TypeError, ValueError, UnicodeError):
        raise DispatchScadaParseError(
            f"row {max(reader.line_num, 1)}: malformed CSV"
        ) from None


def _parse_timestamp(
    value: str,
    naive_timezone: tzinfo | None,
    row_number: int,
) -> datetime:
    try:
        normalized = value.strip().replace("/", "-").replace(" ", "T", 1)
        if "T" not in normalized:
            raise ValueError
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            if naive_timezone is None:
                raise DispatchScadaParseError(
                    f"row {row_number}: offset-free timestamp requires naive_timezone"
                )
            if (
                type(naive_timezone) is not timezone
                or naive_timezone.utcoffset(None) != timedelta(hours=10)
            ):
                raise DispatchScadaParseError(
                    f"row {row_number}: invalid naive_timezone"
                )
            parsed = parsed.replace(tzinfo=naive_timezone)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except DispatchScadaParseError:
        raise
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise DispatchScadaParseError(f"row {row_number}: invalid timestamp") from None


def _parse_power(value: str, row_number: int) -> float:
    try:
        return float(value.strip())
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise DispatchScadaParseError(f"row {row_number}: invalid power value") from None


def _record_from_row(
    row: list[str],
    row_number: int,
    *,
    source_artifact_id: str,
    ingestion_version: int,
    correction_version: int,
    naive_timezone: tzinfo | None,
) -> GeneratorPower5m:
    try:
        return GeneratorPower5m(
            generator_id=row[5].strip(),
            interval_start=_parse_timestamp(row[4], naive_timezone, row_number),
            power_mw=_parse_power(row[6], row_number),
            source_id=source_artifact_id,
            source_timestamp=_parse_timestamp(row[7], naive_timezone, row_number),
            ingestion_version=ingestion_version,
            correction_version=correction_version,
        )
    except DispatchScadaParseError:
        raise
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise DispatchScadaParseError(f"row {row_number}: invalid data fields") from None


def _validate_metadata(
    row: list[str],
    *,
    source_artifact_id: str,
) -> None:
    if len(row) != 10:
        raise DispatchScadaParseError("row 1: invalid metadata field count")
    if tuple(row[:5]) != _METADATA_PREFIX or row[8] != "DISPATCHSCADA":
        raise DispatchScadaParseError("row 1: invalid metadata structure")
    if row[7] != source_artifact_id:
        raise DispatchScadaParseError("row 1: source sequence does not match artifact")
    if not row[5].strip() or not row[6].strip() or not row[9].strip():
        raise DispatchScadaParseError("row 1: malformed metadata fields")
    try:
        datetime.strptime(f"{row[5]} {row[6]}", "%Y/%m/%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        raise DispatchScadaParseError("row 1: invalid metadata timestamp") from None


def _validate_header(row: list[str]) -> None:
    if len(row) != len(_HEADER):
        raise DispatchScadaParseError("row 2: invalid header field count")
    if tuple(row) != _HEADER:
        raise DispatchScadaParseError("row 2: invalid header")


def _validate_footer(rows: list[list[str]]) -> None:
    row_number = len(rows)
    footer = rows[-1]
    if len(footer) != 3 or tuple(footer[:2]) != _FOOTER_PREFIX:
        raise DispatchScadaParseError(f"row {row_number}: invalid footer")
    try:
        footer_count = int(footer[2])
    except (TypeError, ValueError, OverflowError):
        raise DispatchScadaParseError(f"row {row_number}: invalid footer count") from None
    if str(footer_count) != footer[2] or footer_count != len(rows):
        raise DispatchScadaParseError(f"row {row_number}: footer count mismatch")


def parse_dispatch_scada_csv(
    payload: str,
    *,
    source_artifact_id: str,
    ingestion_version: int,
    correction_version: int = 0,
    naive_timezone: tzinfo | None = None,
) -> tuple[GeneratorPower5m, ...]:
    """Parse one complete canonical Dispatch SCADA CSV member.

    The returned records are UTC-normalized and sorted by their logical key.
    Offset-free source timestamps require an explicit ``naive_timezone``.
    """

    rows = _read_rows(payload)
    if not rows:
        raise DispatchScadaParseError("row 1: missing metadata row")
    _validate_metadata(rows[0], source_artifact_id=source_artifact_id)
    if len(rows) < 2:
        raise DispatchScadaParseError("row 2: missing header row")
    _validate_header(rows[1])
    if len(rows) < 3:
        raise DispatchScadaParseError("row 3: missing footer row")
    _validate_footer(rows)

    data_rows = rows[2:-1]
    if not data_rows:
        raise DispatchScadaParseError("row 3: report contains no data rows")

    records: list[GeneratorPower5m] = []
    seen_keys: set[tuple[str, datetime]] = set()
    for row_number, row in enumerate(data_rows, start=3):
        if len(row) != 8:
            raise DispatchScadaParseError(f"row {row_number}: invalid data field count")
        if tuple(row[:4]) != _DATA_PREFIX:
            raise DispatchScadaParseError(f"row {row_number}: invalid data structure")
        record = _record_from_row(
            row,
            row_number,
            source_artifact_id=source_artifact_id,
            ingestion_version=ingestion_version,
            correction_version=correction_version,
            naive_timezone=naive_timezone,
        )
        if record.logical_key in seen_keys:
            raise DispatchScadaParseError(f"row {row_number}: duplicate logical key")
        seen_keys.add(record.logical_key)
        records.append(record)

    return tuple(sorted(records, key=lambda record: record.logical_key))
