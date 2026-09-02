"""Strict parser for canonical AEMO dispatch-price CSV rows."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone, tzinfo
from io import StringIO
from math import isfinite

from .storage import RegionalPrice5m


_REQUIRED_COLUMNS = {"SETTLEMENTDATE", "REGIONID", "RRP"}
_OPTIONAL_FLAG_COLUMNS = ("INTERVENTION", "APCFLAG", "RUNNO")


class AemoParseError(ValueError):
    """Raised when a dispatch-price payload cannot be safely normalized."""


def _parse_timestamp(value: str, *, naive_timezone: tzinfo | None) -> datetime:
    normalized = value.strip().replace("/", "-").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AemoParseError(f"invalid SETTLEMENTDATE: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if naive_timezone is None:
            raise AemoParseError("naive SETTLEMENTDATE requires naive_timezone")
        parsed = parsed.replace(tzinfo=naive_timezone)
    return parsed


def _parse_nonnegative_flag(value: str, name: str, row_number: int) -> int:
    if not value.strip():
        return 0
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AemoParseError(f"row {row_number}: {name} must be an integer") from exc
    if parsed < 0:
        raise AemoParseError(f"row {row_number}: {name} must be non-negative")
    return parsed


def _parse_market_suspended(value: str, row_number: int) -> bool:
    parsed = _parse_nonnegative_flag(value, "MARKETSUSPENDEDFLAG", row_number)
    if parsed not in (0, 1):
        raise AemoParseError(
            f"row {row_number}: MARKETSUSPENDEDFLAG must be 0 or 1"
        )
    return bool(parsed)


def _quality_flags(row: dict[str, str], row_number: int) -> tuple[str, ...]:
    flags: list[str] = []
    for name in _OPTIONAL_FLAG_COLUMNS:
        value = row.get(name, "")
        if name == "RUNNO":
            runno = _parse_nonnegative_flag(value, name, row_number)
            flags.append(f"runno={runno}")
        else:
            flag = _parse_nonnegative_flag(value, name, row_number)
            if flag:
                flags.append(f"{name.lower()}={flag}")
    return tuple(flags)


def _parse_price(value: str, row_number: int) -> float | None:
    if not value.strip():
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise AemoParseError(f"row {row_number}: RRP must be numeric or blank") from exc


def parse_dispatch_price_csv(
    payload: str,
    *,
    source_id: str,
    source_timestamp: datetime,
    ingestion_version: int,
    correction_version: int = 0,
    naive_timezone: tzinfo | None = None,
) -> tuple[RegionalPrice5m, ...]:
    """Parse strict dispatch-price rows into UTC-normalized price records.

    AEMO CSV timestamps without an offset are accepted only when the caller
    supplies the source timezone explicitly. Revision values are supplied by
    the ingestion coordinator; ``RUNNO`` is retained as provenance metadata.
    """

    reader = csv.DictReader(StringIO(payload))
    fieldnames = reader.fieldnames
    if not fieldnames or len(fieldnames) != len(set(fieldnames)):
        raise AemoParseError("CSV must have a unique header row")
    missing = _REQUIRED_COLUMNS - set(fieldnames)
    if missing:
        raise AemoParseError(f"CSV is missing required columns: {sorted(missing)}")

    records: list[RegionalPrice5m] = []
    seen: set[tuple[str, datetime]] = set()
    for row_number, row in enumerate(reader, start=2):
        if None in row or any(value is None for value in row.values()):
            raise AemoParseError(f"row {row_number}: malformed CSV fields")
        region = row["REGIONID"].strip()
        if not region:
            raise AemoParseError(f"row {row_number}: REGIONID is required")
        try:
            interval_start = _parse_timestamp(
                row["SETTLEMENTDATE"], naive_timezone=naive_timezone
            )
            price = _parse_price(row["RRP"], row_number)
            record = RegionalPrice5m(
                region=region,
                interval_start=interval_start,
                price_aud_per_mwh=price,
                price_status="missing" if price is None else ("negative" if price < 0 else "available"),
                source_id=source_id,
                source_timestamp=source_timestamp,
                ingestion_version=ingestion_version,
                correction_version=correction_version,
                quality_flags=_quality_flags(row, row_number),
                intervention=_parse_nonnegative_flag(
                    row.get("INTERVENTION", ""), "INTERVENTION", row_number
                ),
                apc_flag=_parse_nonnegative_flag(
                    row.get("APCFLAG", ""), "APCFLAG", row_number
                ),
                market_suspended=_parse_market_suspended(
                    row.get("MARKETSUSPENDEDFLAG", ""), row_number
                ),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, AemoParseError):
                raise
            raise AemoParseError(f"row {row_number}: {exc}") from exc
        if record.logical_key in seen:
            raise AemoParseError(f"row {row_number}: duplicate region and interval")
        seen.add(record.logical_key)
        records.append(record)

    return tuple(sorted(records, key=lambda record: record.logical_key))


_NEM_TIMEZONE = timezone(timedelta(hours=10))
_MMS_METADATA_PREFIX = ("C", "NEMP.WORLD", "DISPATCHIS", "AEMO", "PUBLIC")
_MMS_PRICE_HEADER = (
    "I", "DISPATCH", "PRICE", "5", "SETTLEMENTDATE", "RUNNO", "REGIONID",
    "DISPATCHINTERVAL", "INTERVENTION", "RRP", "EEP", "ROP", "APCFLAG",
    "MARKETSUSPENDEDFLAG", "LASTCHANGED", "RAISE6SECRRP", "RAISE6SECROP",
    "RAISE6SECAPCFLAG", "RAISE60SECRRP", "RAISE60SECROP", "RAISE60SECAPCFLAG",
    "RAISE5MINRRP", "RAISE5MINROP", "RAISE5MINAPCFLAG", "RAISEREGRRP",
    "RAISEREGROP", "RAISEREGAPCFLAG", "LOWER6SECRRP", "LOWER6SECROP",
    "LOWER6SECAPCFLAG", "LOWER60SECRRP", "LOWER60SECROP", "LOWER60SECAPCFLAG",
    "LOWER5MINRRP", "LOWER5MINROP", "LOWER5MINAPCFLAG", "LOWERREGRRP",
    "LOWERREGROP", "LOWERREGAPCFLAG", "PRICE_STATUS", "PRE_AP_ENERGY_PRICE",
    "PRE_AP_RAISE6_PRICE", "PRE_AP_RAISE60_PRICE", "PRE_AP_RAISE5MIN_PRICE",
    "PRE_AP_RAISEREG_PRICE", "PRE_AP_LOWER6_PRICE", "PRE_AP_LOWER60_PRICE",
    "PRE_AP_LOWER5MIN_PRICE", "PRE_AP_LOWERREG_PRICE", "RAISE1SECRRP",
    "RAISE1SECROP", "RAISE1SECAPCFLAG", "LOWER1SECRRP", "LOWER1SECROP",
    "LOWER1SECAPCFLAG", "PRE_AP_RAISE1_PRICE", "PRE_AP_LOWER1_PRICE",
    "CUMUL_PRE_AP_ENERGY_PRICE", "CUMUL_PRE_AP_RAISE6_PRICE",
    "CUMUL_PRE_AP_RAISE60_PRICE", "CUMUL_PRE_AP_RAISE5MIN_PRICE",
    "CUMUL_PRE_AP_RAISEREG_PRICE", "CUMUL_PRE_AP_LOWER6_PRICE",
    "CUMUL_PRE_AP_LOWER60_PRICE", "CUMUL_PRE_AP_LOWER5MIN_PRICE",
    "CUMUL_PRE_AP_LOWERREG_PRICE", "CUMUL_PRE_AP_RAISE1_PRICE",
    "CUMUL_PRE_AP_LOWER1_PRICE", "OCD_STATUS", "MII_STATUS",
)
_MMS_REGIONS = frozenset(("NSW1", "QLD1", "SA1", "TAS1", "VIC1"))
_MMS_PRICE_STATUSES = frozenset(("FIRM", "NOT FIRM"))


def _mms_timestamp(value: str, row_number: int, *, aligned: bool) -> datetime:
    normalized = value.strip().replace("/", "-").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AemoParseError(f"row {row_number}: invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_NEM_TIMEZONE)
    result = parsed.astimezone(timezone.utc)
    if aligned and (result.minute % 5 or result.second or result.microsecond):
        raise AemoParseError(f"row {row_number}: timestamp is not five-minute aligned")
    return result


def _mms_control(value: str, name: str, row_number: int) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise AemoParseError(f"row {row_number}: invalid {name}")
    parsed = int(value)
    if parsed > 2_147_483_647:
        raise AemoParseError(f"row {row_number}: invalid {name}")
    if name == "MARKETSUSPENDEDFLAG" and parsed not in (0, 1):
        raise AemoParseError(f"row {row_number}: invalid {name}")
    return parsed


def _mms_rrp(value: str, row_number: int) -> float | None:
    if not value.strip():
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AemoParseError(f"row {row_number}: invalid RRP") from exc
    if not isfinite(parsed):
        raise AemoParseError(f"row {row_number}: invalid RRP")
    return parsed


def parse_dispatch_price_mms_csv(
    payload: str,
    *,
    source_id: str,
    ingestion_version: int,
    correction_version: int = 0,
    source_timestamp: datetime | None = None,
) -> tuple[RegionalPrice5m, ...]:
    """Parse one complete version-5 MMS DispatchIS regional-price report."""

    if not isinstance(payload, str) or not payload or "\x00" in payload:
        raise AemoParseError("invalid MMS CSV")
    if not isinstance(source_id, str) or not source_id:
        raise AemoParseError("invalid source id")
    try:
        rows = list(csv.reader(StringIO(payload), strict=True))
    except csv.Error as exc:
        raise AemoParseError("invalid MMS CSV") from exc
    if len(rows) < 4 or any(not row for row in rows):
        raise AemoParseError("invalid MMS envelope")

    metadata = rows[0]
    if (
        len(metadata) != 10
        or tuple(metadata[:5]) != _MMS_METADATA_PREFIX
        or metadata[7] != source_id
        or metadata[8] != "DISPATCHIS"
        or not metadata[9].isascii()
        or not metadata[9].isdecimal()
    ):
        raise AemoParseError("invalid MMS envelope")
    try:
        datetime.strptime(f"{metadata[5]} {metadata[6]}", "%Y/%m/%d %H:%M:%S")
    except ValueError as exc:
        raise AemoParseError("invalid MMS envelope") from exc

    footer = rows[-1]
    if len(footer) != 3 or tuple(footer[:2]) != ("C", "END OF REPORT"):
        raise AemoParseError("invalid MMS envelope")
    try:
        footer_count = int(footer[2])
    except ValueError as exc:
        raise AemoParseError("invalid MMS envelope") from exc
    if str(footer_count) != footer[2] or footer_count != len(rows):
        raise AemoParseError("invalid MMS envelope")

    header_positions: dict[str, int] | None = None
    data_rows: list[tuple[int, list[str]]] = []
    for row_number, row in enumerate(rows[1:-1], start=2):
        if row[0] == "I" and len(row) >= 3 and tuple(row[1:3]) == ("DISPATCH", "PRICE"):
            if tuple(row) != _MMS_PRICE_HEADER or header_positions is not None:
                raise AemoParseError(f"row {row_number}: invalid PRICE header")
            header_positions = {name: index for index, name in enumerate(row)}
        elif row[0] == "D" and len(row) >= 3 and tuple(row[1:3]) == ("DISPATCH", "PRICE"):
            if len(row) < 4 or row[3] != "5":
                raise AemoParseError(f"row {row_number}: unsupported PRICE version")
            if header_positions is None:
                raise AemoParseError(f"row {row_number}: PRICE data precedes header")
            data_rows.append((row_number, row))
        elif row[0] not in ("C", "I", "D"):
            raise AemoParseError(f"row {row_number}: invalid MMS row type")

    if header_positions is None or len(data_rows) != len(_MMS_REGIONS):
        raise AemoParseError("PRICE report must contain exactly five NEM regions")

    records: list[RegionalPrice5m] = []
    seen: set[tuple[str, datetime]] = set()
    interval: datetime | None = None
    for row_number, row in data_rows:
        if len(row) != len(_MMS_PRICE_HEADER) or tuple(row[:4]) != ("D", "DISPATCH", "PRICE", "5"):
            raise AemoParseError(f"row {row_number}: invalid PRICE row")
        region = row[header_positions["REGIONID"]]
        if region not in _MMS_REGIONS:
            raise AemoParseError(f"row {row_number}: unknown NEM region")
        settlement = _mms_timestamp(
            row[header_positions["SETTLEMENTDATE"]], row_number, aligned=True
        )
        if interval is None:
            interval = settlement
        elif settlement != interval:
            raise AemoParseError(f"row {row_number}: mixed settlement intervals")
        key = (region, settlement)
        if key in seen:
            raise AemoParseError(f"row {row_number}: duplicate region and interval")
        seen.add(key)

        runno = _mms_control(row[header_positions["RUNNO"]], "RUNNO", row_number)
        intervention = _mms_control(
            row[header_positions["INTERVENTION"]], "INTERVENTION", row_number
        )
        apc_flag = _mms_control(row[header_positions["APCFLAG"]], "APCFLAG", row_number)
        suspended = _mms_control(
            row[header_positions["MARKETSUSPENDEDFLAG"]],
            "MARKETSUSPENDEDFLAG",
            row_number,
        )
        status = row[header_positions["PRICE_STATUS"]]
        if status not in _MMS_PRICE_STATUSES:
            raise AemoParseError(f"row {row_number}: invalid PRICE_STATUS")
        price = _mms_rrp(row[header_positions["RRP"]], row_number)
        observed_at = source_timestamp or _mms_timestamp(
            row[header_positions["LASTCHANGED"]], row_number, aligned=False
        )
        flags = [f"runno={runno}"]
        if intervention:
            flags.append(f"intervention={intervention}")
        if apc_flag:
            flags.append(f"apcflag={apc_flag}")
        if suspended:
            flags.append("market_suspended=1")
        flags.append(f"aemo_price_status={status}")
        try:
            records.append(RegionalPrice5m(
                region=region,
                interval_start=settlement,
                price_aud_per_mwh=price,
                price_status=(
                    "missing" if price is None else
                    ("negative" if price < 0 else "available")
                ),
                source_id=source_id,
                source_timestamp=observed_at,
                ingestion_version=ingestion_version,
                correction_version=correction_version,
                quality_flags=tuple(flags),
                intervention=intervention,
                apc_flag=apc_flag,
                market_suspended=bool(suspended),
            ))
        except (TypeError, ValueError) as exc:
            raise AemoParseError(f"row {row_number}: invalid PRICE fields") from exc

    if {record.region for record in records} != _MMS_REGIONS:
        raise AemoParseError("PRICE report must contain exactly five NEM regions")
    return tuple(sorted(records, key=lambda record: record.logical_key))


__all__ = [
    "AemoParseError",
    "parse_dispatch_price_csv",
    "parse_dispatch_price_mms_csv",
]
