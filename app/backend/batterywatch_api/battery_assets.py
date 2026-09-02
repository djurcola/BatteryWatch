
"""Strict loader for reviewed BatteryWatch DUID metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import re


_TOP_LEVEL_KEYS = {"schema_version", "assets", "excluded"}
_ASSET_KEYS = {
    "duid",
    "site_name",
    "region",
    "capacity_mw",
    "storage_capacity_mwh",
    "source_id",
    "source_timestamp",
}
_EXCLUDED_KEYS = {"duid", "reason"}
_REGIONS = {"NSW1", "QLD1", "SA1", "TAS1", "VIC1"}
_DUID_RE = re.compile(r"^[A-Z0-9_]{1,32}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True, slots=True)
class BatteryAsset:
    duid: str
    site_name: str
    region: str
    capacity_mw: float
    storage_capacity_mwh: float
    source_id: str
    source_timestamp: datetime


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"invalid {field}")
    return value


def _positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {field}")
    normalized = float(value)
    if not isfinite(normalized) or normalized <= 0:
        raise ValueError(f"invalid {field}")
    return normalized


def _timestamp(value: object) -> datetime:
    text = _text(value, "source_timestamp")
    if not text.endswith("Z"):
        raise ValueError("invalid source_timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        raise ValueError("invalid source_timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid source_timestamp")
    return parsed.astimezone(timezone.utc)


def load_battery_assets(path: str | Path) -> tuple[BatteryAsset, ...]:
    """Load one exact reviewed config and reject ambiguity fail closed."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid battery asset config") from exc
    if type(payload) is not dict or set(payload) != _TOP_LEVEL_KEYS:
        raise ValueError("invalid battery asset config")
    if payload["schema_version"] != 1:
        raise ValueError("invalid battery asset schema version")
    raw_assets = payload["assets"]
    raw_excluded = payload["excluded"]
    if type(raw_assets) is not list or not raw_assets or type(raw_excluded) is not list:
        raise ValueError("invalid battery asset config")

    excluded: set[str] = set()
    for item in raw_excluded:
        if type(item) is not dict or set(item) != _EXCLUDED_KEYS:
            raise ValueError("invalid excluded battery asset")
        duid = _text(item["duid"], "excluded duid")
        _text(item["reason"], "excluded reason")
        if not _DUID_RE.fullmatch(duid) or duid in excluded:
            raise ValueError("invalid excluded battery asset")
        excluded.add(duid)

    assets: list[BatteryAsset] = []
    seen: set[str] = set()
    for item in raw_assets:
        if type(item) is not dict or set(item) != _ASSET_KEYS:
            raise ValueError("invalid battery asset")
        duid = _text(item["duid"], "duid")
        site_name = _text(item["site_name"], "site_name")
        region = _text(item["region"], "region")
        source_id = _text(item["source_id"], "source_id")
        if (
            not _DUID_RE.fullmatch(duid)
            or region not in _REGIONS
            or not _SOURCE_ID_RE.fullmatch(source_id)
            or duid in seen
            or duid in excluded
        ):
            raise ValueError("invalid battery asset")
        seen.add(duid)
        assets.append(
            BatteryAsset(
                duid=duid,
                site_name=site_name,
                region=region,
                capacity_mw=_positive_number(item["capacity_mw"], "capacity_mw"),
                storage_capacity_mwh=_positive_number(
                    item["storage_capacity_mwh"], "storage_capacity_mwh"
                ),
                source_id=source_id,
                source_timestamp=_timestamp(item["source_timestamp"]),
            )
        )
    return tuple(sorted(assets, key=lambda asset: asset.duid))


__all__ = ["BatteryAsset", "load_battery_assets"]
