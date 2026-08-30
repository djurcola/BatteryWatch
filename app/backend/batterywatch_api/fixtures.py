"""Deterministic five-minute fixture data for the first API slice."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict

FIXTURE_DUID = "BWTEST1"
FIXTURE_REGION = "NSW1"
INTERVAL = timedelta(minutes=5)
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class FixturePoint:
    timestamp: datetime
    power_mw: float
    price_aud_per_mwh: float | None
    soc_percent: float | None


class GeneratorMetadata(TypedDict):
    duid: str
    site_name: str
    region: str
    capacity_mw: float
    storage_capacity_mwh: float
    data_start: datetime
    data_end: datetime
    data_status: Literal["fixture"]


# The first five intervals intentionally exercise discharge, charging, zero,
# negative-price discharge, and missing-price handling.
_POWER = (1.5, -1.0, 0.0, 2.0, 1.0, 0.0, -0.5, 0.75, 0.0, 1.25, -0.75, 0.0)
_PRICE = (100.0, 80.0, 50.0, -20.0, None, 40.0, 30.0, 120.0, 25.0, 90.0, 70.0, 60.0)
_SOC = (45.0, 42.0, 42.0, 48.0, None, 50.0, 47.0, 49.0, 49.0, 53.0, 51.0, 51.0)


def generator_metadata() -> GeneratorMetadata:
    return {
        "duid": FIXTURE_DUID,
        "site_name": "BatteryWatch Fixture",
        "region": FIXTURE_REGION,
        "capacity_mw": 2.0,
        "storage_capacity_mwh": 4.0,
        "data_start": START,
        "data_end": START + len(_POWER) * INTERVAL,
        "data_status": "fixture",
    }


def fixture_points() -> list[FixturePoint]:
    return [
        FixturePoint(START + index * INTERVAL, power, price, soc)
        for index, (power, price, soc) in enumerate(zip(_POWER, _PRICE, _SOC))
    ]
