"""Persistence contracts and repository implementations for BatteryWatch.

The repositories store one effective record per logical generator/region interval.
The PostgreSQL adapter uses a DB-API-compatible connection supplied by its caller;
it deliberately does not import a driver or create a connection by itself.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from collections.abc import Hashable, Iterable, Iterator, MutableMapping
from typing import Any, Literal, Protocol, TypeVar

UTC = timezone.utc
PriceStatus = Literal["available", "negative", "missing"]
_VALID_PRICE_STATUSES = {"available", "negative", "missing"}


def utc_timestamp(value: datetime, field: str = "timestamp") -> datetime:
    """Return an aware timestamp in UTC; reject ambiguous naive timestamps."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return value.astimezone(UTC)


def aligned_5m(value: datetime, field: str = "interval_start") -> datetime:
    """Normalize an interval start to UTC and require an exact five-minute boundary."""

    normalized = utc_timestamp(value, field)
    if normalized.minute % 5 or normalized.second or normalized.microsecond:
        raise ValueError(f"{field} must be aligned to a five-minute boundary")
    return normalized


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _number(value: float, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not isfinite(normalized):
        raise ValueError(f"{field} must be finite")
    return normalized


def _optional_number(value: float | None, field: str) -> float | None:
    return None if value is None else _number(value, field)


def _version(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _flags(value: Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ValueError("quality_flags must be an iterable of strings, not a string")
    normalized = tuple(_text(flag, "quality flag") for flag in value)
    return normalized


def _provenance_values(
    source_id: str,
    source_timestamp: datetime,
    ingestion_version: int,
    correction_version: int,
) -> tuple[str, datetime, int, int]:
    return (
        _text(source_id, "source_id"),
        utc_timestamp(source_timestamp, "source_timestamp"),
        _version(ingestion_version, "ingestion_version"),
        _version(correction_version, "correction_version"),
    )


@dataclass(frozen=True, slots=True)
class RecordProvenance:
    """Source and revision information retained with every persisted record."""

    source_id: str
    source_timestamp: datetime
    ingestion_version: int
    correction_version: int = 0

    def __post_init__(self) -> None:
        source_id, source_timestamp, ingestion, correction = _provenance_values(
            self.source_id,
            self.source_timestamp,
            self.ingestion_version,
            self.correction_version,
        )
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_timestamp", source_timestamp)
        object.__setattr__(self, "ingestion_version", ingestion)
        object.__setattr__(self, "correction_version", correction)


class _VersionedRecord:
    __slots__ = ()

    source_id: str
    source_timestamp: datetime
    ingestion_version: int
    correction_version: int

    @property
    def provenance(self) -> RecordProvenance:
        return RecordProvenance(
            source_id=self.source_id,
            source_timestamp=self.source_timestamp,
            ingestion_version=self.ingestion_version,
            correction_version=self.correction_version,
        )


@dataclass(frozen=True, slots=True)
class GeneratorMetadata(_VersionedRecord):
    """Stable generator metadata and its registry provenance."""

    generator_id: str
    site_name: str
    region: str
    capacity_mw: float
    storage_capacity_mwh: float
    source_id: str
    source_timestamp: datetime
    ingestion_version: int
    correction_version: int = 0
    data_start: datetime | None = None
    data_end: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "generator_id", _text(self.generator_id, "generator_id"))
        object.__setattr__(self, "site_name", _text(self.site_name, "site_name"))
        object.__setattr__(self, "region", _text(self.region, "region"))
        object.__setattr__(self, "capacity_mw", _number(self.capacity_mw, "capacity_mw"))
        object.__setattr__(
            self,
            "storage_capacity_mwh",
            _number(self.storage_capacity_mwh, "storage_capacity_mwh"),
        )
        if self.capacity_mw <= 0 or self.storage_capacity_mwh <= 0:
            raise ValueError("generator capacities must be positive")
        source_id, source_timestamp, ingestion, correction = _provenance_values(
            self.source_id,
            self.source_timestamp,
            self.ingestion_version,
            self.correction_version,
        )
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_timestamp", source_timestamp)
        object.__setattr__(self, "ingestion_version", ingestion)
        object.__setattr__(self, "correction_version", correction)
        start = None if self.data_start is None else utc_timestamp(self.data_start, "data_start")
        end = None if self.data_end is None else utc_timestamp(self.data_end, "data_end")
        if start is not None and end is not None and end <= start:
            raise ValueError("data_end must be after data_start")
        object.__setattr__(self, "data_start", start)
        object.__setattr__(self, "data_end", end)

    @property
    def logical_key(self) -> tuple[str]:
        return (self.generator_id,)

    @property
    def duid(self) -> str:
        """NEM terminology alias for the stable generator identity."""

        return self.generator_id


@dataclass(frozen=True, slots=True)
class GeneratorPower5m(_VersionedRecord):
    """Five-minute generator power; positive exports and negative imports."""

    generator_id: str
    interval_start: datetime
    power_mw: float
    source_id: str
    source_timestamp: datetime
    ingestion_version: int
    correction_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "generator_id", _text(self.generator_id, "generator_id"))
        object.__setattr__(self, "interval_start", aligned_5m(self.interval_start))
        object.__setattr__(self, "power_mw", _number(self.power_mw, "power_mw"))
        source_id, source_timestamp, ingestion, correction = _provenance_values(
            self.source_id,
            self.source_timestamp,
            self.ingestion_version,
            self.correction_version,
        )
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_timestamp", source_timestamp)
        object.__setattr__(self, "ingestion_version", ingestion)
        object.__setattr__(self, "correction_version", correction)

    @property
    def logical_key(self) -> tuple[str, datetime]:
        return (self.generator_id, self.interval_start)

    @property
    def timestamp(self) -> datetime:
        return self.interval_start


@dataclass(frozen=True, slots=True)
class GeneratorSoc5m(_VersionedRecord):
    """Five-minute SOC observation; ``None`` means unavailable, never inferred."""

    generator_id: str
    interval_start: datetime
    soc_percent: float | None
    source_id: str
    source_timestamp: datetime
    ingestion_version: int
    correction_version: int = 0
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "generator_id", _text(self.generator_id, "generator_id"))
        object.__setattr__(self, "interval_start", aligned_5m(self.interval_start))
        soc = _optional_number(self.soc_percent, "soc_percent")
        if soc is not None and not 0 <= soc <= 100:
            raise ValueError("soc_percent must be between 0 and 100")
        object.__setattr__(self, "soc_percent", soc)
        source_id, source_timestamp, ingestion, correction = _provenance_values(
            self.source_id,
            self.source_timestamp,
            self.ingestion_version,
            self.correction_version,
        )
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_timestamp", source_timestamp)
        object.__setattr__(self, "ingestion_version", ingestion)
        object.__setattr__(self, "correction_version", correction)
        object.__setattr__(self, "quality_flags", _flags(self.quality_flags))

    @property
    def logical_key(self) -> tuple[str, datetime]:
        return (self.generator_id, self.interval_start)

    @property
    def timestamp(self) -> datetime:
        return self.interval_start

    @property
    def is_available(self) -> bool:
        return self.soc_percent is not None

    @property
    def soc_status(self) -> Literal["available", "missing"]:
        return "available" if self.is_available else "missing"


@dataclass(frozen=True, slots=True)
class RegionalPrice5m(_VersionedRecord):
    """Five-minute regional price with explicit missing/negative status and flags."""

    region: str
    interval_start: datetime
    price_aud_per_mwh: float | None
    price_status: PriceStatus
    source_id: str
    source_timestamp: datetime
    ingestion_version: int
    correction_version: int = 0
    quality_flags: tuple[str, ...] = ()
    intervention: int = 0
    apc_flag: int = 0
    market_suspended: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "region", _text(self.region, "region"))
        object.__setattr__(self, "interval_start", aligned_5m(self.interval_start))
        if self.price_status not in _VALID_PRICE_STATUSES:
            raise ValueError("price_status must be available, negative, or missing")
        price = _optional_number(self.price_aud_per_mwh, "price_aud_per_mwh")
        expected_status = "missing" if price is None else ("negative" if price < 0 else "available")
        if self.price_status != expected_status:
            raise ValueError("price_status does not match price_aud_per_mwh")
        object.__setattr__(self, "price_aud_per_mwh", price)
        source_id, source_timestamp, ingestion, correction = _provenance_values(
            self.source_id,
            self.source_timestamp,
            self.ingestion_version,
            self.correction_version,
        )
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_timestamp", source_timestamp)
        object.__setattr__(self, "ingestion_version", ingestion)
        object.__setattr__(self, "correction_version", correction)
        object.__setattr__(self, "quality_flags", _flags(self.quality_flags))
        object.__setattr__(self, "intervention", _version(self.intervention, "intervention"))
        object.__setattr__(self, "apc_flag", _version(self.apc_flag, "apc_flag"))
        if not isinstance(self.market_suspended, bool):
            raise ValueError("market_suspended must be a boolean")

    @property
    def logical_key(self) -> tuple[str, datetime]:
        return (self.region, self.interval_start)

    @property
    def timestamp(self) -> datetime:
        return self.interval_start

    @property
    def status(self) -> PriceStatus:
        return self.price_status

    @property
    def is_available(self) -> bool:
        return self.price_aud_per_mwh is not None

    @property
    def is_missing(self) -> bool:
        return not self.is_available

    @property
    def is_negative(self) -> bool:
        return self.price_aud_per_mwh is not None and self.price_aud_per_mwh < 0


# Short aliases make the contract convenient without losing the table-shaped names.
GeneratorRecord = GeneratorMetadata
PowerRecord = GeneratorPower5m
SocRecord = GeneratorSoc5m
PriceRecord = RegionalPrice5m


class StorageRepository(Protocol):
    """Replaceable persistence boundary used by later database-backed slices."""

    def upsert_generator(self, record: GeneratorMetadata) -> bool: ...

    def upsert_power(self, record: GeneratorPower5m) -> bool: ...

    def upsert_soc(self, record: GeneratorSoc5m) -> bool: ...

    def upsert_price(self, record: RegionalPrice5m) -> bool: ...

    def read_generator(self, generator_id: str) -> GeneratorMetadata | None: ...

    def list_generators(self) -> tuple[GeneratorMetadata, ...]: ...

    def list_power(
        self,
        generator_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[GeneratorPower5m, ...]: ...

    def list_soc(
        self,
        generator_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[GeneratorSoc5m, ...]: ...

    def list_prices(
        self,
        region: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[RegionalPrice5m, ...]: ...

    def read_power(self, generator_id: str, interval_start: datetime) -> GeneratorPower5m | None: ...

    def read_soc(self, generator_id: str, interval_start: datetime) -> GeneratorSoc5m | None: ...

    def read_price(self, region: str, interval_start: datetime) -> RegionalPrice5m | None: ...


Repository = StorageRepository
_Key = TypeVar("_Key", bound=Hashable)
_Record = TypeVar("_Record", bound=_VersionedRecord)


def _revision(record: _VersionedRecord) -> tuple[int, int, datetime]:
    return (record.correction_version, record.ingestion_version, record.source_timestamp)


def _upsert(store: MutableMapping[_Key, _Record], key: _Key, record: _Record) -> bool:
    current = store.get(key)
    if current is not None and _revision(record) <= _revision(current):
        return False
    store[key] = record
    return True


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime | None, datetime | None]:
    normalized_start = None if start is None else utc_timestamp(start, "start")
    normalized_end = None if end is None else utc_timestamp(end, "end")
    if normalized_start is not None and normalized_end is not None and normalized_end < normalized_start:
        raise ValueError("end must not precede start")
    return normalized_start, normalized_end


class InMemoryRepository:
    """Deterministic one-effective-record repository for tests and local seams.

    Upsert returns ``True`` for a new or newer effective record and ``False`` for
    an exact replay or a stale revision.  Logical keys contain only the stable
    dimension and aligned interval; provenance remains on the stored value.
    """

    def __init__(self) -> None:
        self._generators: dict[tuple[str], GeneratorMetadata] = {}
        self._power: dict[tuple[str, datetime], GeneratorPower5m] = {}
        self._soc: dict[tuple[str, datetime], GeneratorSoc5m] = {}
        self._prices: dict[tuple[str, datetime], RegionalPrice5m] = {}

    def upsert_generator(self, record: GeneratorMetadata) -> bool:
        return _upsert(self._generators, record.logical_key, record)

    insert_generator = upsert_generator

    def upsert_power(self, record: GeneratorPower5m) -> bool:
        return _upsert(self._power, record.logical_key, record)

    insert_power = upsert_power

    def upsert_soc(self, record: GeneratorSoc5m) -> bool:
        return _upsert(self._soc, record.logical_key, record)

    insert_soc = upsert_soc

    def upsert_price(self, record: RegionalPrice5m) -> bool:
        return _upsert(self._prices, record.logical_key, record)

    insert_price = upsert_price

    def read_generator(self, generator_id: str) -> GeneratorMetadata | None:
        return self._generators.get((_text(generator_id, "generator_id"),))

    def read_power(self, generator_id: str, interval_start: datetime) -> GeneratorPower5m | None:
        return self._power.get((_text(generator_id, "generator_id"), aligned_5m(interval_start)))

    def read_soc(self, generator_id: str, interval_start: datetime) -> GeneratorSoc5m | None:
        return self._soc.get((_text(generator_id, "generator_id"), aligned_5m(interval_start)))

    def read_price(self, region: str, interval_start: datetime) -> RegionalPrice5m | None:
        return self._prices.get((_text(region, "region"), aligned_5m(interval_start)))

    def list_generators(self) -> tuple[GeneratorMetadata, ...]:
        return tuple(self._generators[key] for key in sorted(self._generators))

    def list_power(
        self,
        generator_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[GeneratorPower5m, ...]:
        dimension = _text(generator_id, "generator_id")
        normalized_start, normalized_end = _window(start, end)
        values = [
            record
            for (record_dimension, interval_start), record in self._power.items()
            if record_dimension == dimension
            and (normalized_start is None or interval_start >= normalized_start)
            and (normalized_end is None or interval_start < normalized_end)
        ]
        return tuple(sorted(values, key=lambda record: record.interval_start))

    def list_soc(
        self,
        generator_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[GeneratorSoc5m, ...]:
        dimension = _text(generator_id, "generator_id")
        normalized_start, normalized_end = _window(start, end)
        values = [
            record
            for (record_dimension, interval_start), record in self._soc.items()
            if record_dimension == dimension
            and (normalized_start is None or interval_start >= normalized_start)
            and (normalized_end is None or interval_start < normalized_end)
        ]
        return tuple(sorted(values, key=lambda record: record.interval_start))

    def list_prices(
        self,
        region: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[RegionalPrice5m, ...]:
        dimension = _text(region, "region")
        normalized_start, normalized_end = _window(start, end)
        values = [
            record
            for (record_dimension, interval_start), record in self._prices.items()
            if record_dimension == dimension
            and (normalized_start is None or interval_start >= normalized_start)
            and (normalized_end is None or interval_start < normalized_end)
        ]
        return tuple(sorted(values, key=lambda record: record.interval_start))

    def count_power(self, generator_id: str) -> int:
        return len(self.list_power(generator_id))

    def count_soc(self, generator_id: str) -> int:
        return len(self.list_soc(generator_id))

    def count_prices(self, region: str) -> int:
        return len(self.list_prices(region))


class PostgreSQLCursor(Protocol):
    """Small cursor surface required by :class:`PostgreSQLRepository`."""

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> Iterable[tuple[Any, ...]]: ...

    def close(self) -> None: ...


class PostgreSQLConnection(Protocol):
    """Small connection surface accepted by the PostgreSQL adapter."""

    def cursor(self) -> PostgreSQLCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@contextmanager
def _managed_cursor(connection: PostgreSQLConnection) -> Iterator[PostgreSQLCursor]:
    """Yield a cursor and close it without owning the caller's connection."""

    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


_GENERATOR_UPSERT_SQL = """
INSERT INTO generators (
    generator_id,
    site_name,
    region,
    capacity_mw,
    storage_capacity_mwh,
    data_start,
    data_end,
    source_id,
    source_timestamp,
    ingestion_version,
    correction_version
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (generator_id) DO UPDATE
SET site_name = EXCLUDED.site_name,
    region = EXCLUDED.region,
    capacity_mw = EXCLUDED.capacity_mw,
    storage_capacity_mwh = EXCLUDED.storage_capacity_mwh,
    data_start = EXCLUDED.data_start,
    data_end = EXCLUDED.data_end,
    source_id = EXCLUDED.source_id,
    source_timestamp = EXCLUDED.source_timestamp,
    ingestion_version = EXCLUDED.ingestion_version,
    correction_version = EXCLUDED.correction_version,
    updated_at = CURRENT_TIMESTAMP
WHERE (EXCLUDED.correction_version, EXCLUDED.ingestion_version, EXCLUDED.source_timestamp)
    > (generators.correction_version, generators.ingestion_version, generators.source_timestamp)
RETURNING 1
"""

_POWER_UPSERT_SQL = """
INSERT INTO generator_power_5m (
    generator_id,
    interval_start,
    power_mw,
    source_id,
    source_timestamp,
    ingestion_version,
    correction_version
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (generator_id, interval_start) DO UPDATE
SET power_mw = EXCLUDED.power_mw,
    source_id = EXCLUDED.source_id,
    source_timestamp = EXCLUDED.source_timestamp,
    ingestion_version = EXCLUDED.ingestion_version,
    correction_version = EXCLUDED.correction_version
WHERE (EXCLUDED.correction_version, EXCLUDED.ingestion_version, EXCLUDED.source_timestamp)
    > (generator_power_5m.correction_version,
       generator_power_5m.ingestion_version,
       generator_power_5m.source_timestamp)
RETURNING 1
"""

_SOC_UPSERT_SQL = """
INSERT INTO generator_soc_5m (
    generator_id,
    interval_start,
    soc_percent,
    source_id,
    source_timestamp,
    ingestion_version,
    correction_version,
    quality_flags
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (generator_id, interval_start) DO UPDATE
SET soc_percent = EXCLUDED.soc_percent,
    source_id = EXCLUDED.source_id,
    source_timestamp = EXCLUDED.source_timestamp,
    ingestion_version = EXCLUDED.ingestion_version,
    correction_version = EXCLUDED.correction_version,
    quality_flags = EXCLUDED.quality_flags
WHERE (EXCLUDED.correction_version, EXCLUDED.ingestion_version, EXCLUDED.source_timestamp)
    > (generator_soc_5m.correction_version,
       generator_soc_5m.ingestion_version,
       generator_soc_5m.source_timestamp)
RETURNING 1
"""

_PRICE_UPSERT_SQL = """
INSERT INTO nem_price_5m (
    region,
    interval_start,
    price_aud_per_mwh,
    price_status,
    intervention,
    apc_flag,
    market_suspended,
    source_id,
    source_timestamp,
    ingestion_version,
    correction_version,
    quality_flags
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (region, interval_start) DO UPDATE
SET price_aud_per_mwh = EXCLUDED.price_aud_per_mwh,
    price_status = EXCLUDED.price_status,
    intervention = EXCLUDED.intervention,
    apc_flag = EXCLUDED.apc_flag,
    market_suspended = EXCLUDED.market_suspended,
    source_id = EXCLUDED.source_id,
    source_timestamp = EXCLUDED.source_timestamp,
    ingestion_version = EXCLUDED.ingestion_version,
    correction_version = EXCLUDED.correction_version,
    quality_flags = EXCLUDED.quality_flags
WHERE (EXCLUDED.correction_version, EXCLUDED.ingestion_version, EXCLUDED.source_timestamp)
    > (nem_price_5m.correction_version,
       nem_price_5m.ingestion_version,
       nem_price_5m.source_timestamp)
RETURNING 1
"""

_GENERATOR_READ_SQL = """
SELECT generator_id, site_name, region, capacity_mw, storage_capacity_mwh,
       source_id, source_timestamp, ingestion_version, correction_version,
       data_start, data_end
FROM generators
WHERE generator_id = %s
"""

_GENERATORS_LIST_SQL = """
SELECT generator_id, site_name, region, capacity_mw, storage_capacity_mwh,
       source_id, source_timestamp, ingestion_version, correction_version,
       data_start, data_end
FROM generators
ORDER BY generator_id ASC
"""

_POWER_READ_SQL = """
SELECT generator_id, interval_start, power_mw, source_id, source_timestamp,
       ingestion_version, correction_version
FROM generator_power_5m
WHERE generator_id = %s AND interval_start = %s
"""

_SOC_READ_SQL = """
SELECT generator_id, interval_start, soc_percent, source_id, source_timestamp,
       ingestion_version, correction_version, quality_flags
FROM generator_soc_5m
WHERE generator_id = %s AND interval_start = %s
"""

_PRICE_READ_SQL = """
SELECT region, interval_start, price_aud_per_mwh, price_status,
       intervention, apc_flag, market_suspended, source_id, source_timestamp,
       ingestion_version, correction_version, quality_flags
FROM nem_price_5m
WHERE region = %s AND interval_start = %s
"""

_POWER_LIST_SQL = """
SELECT generator_id, interval_start, power_mw, source_id, source_timestamp,
       ingestion_version, correction_version
FROM generator_power_5m
WHERE generator_id = %s
  AND (%s::timestamptz IS NULL OR interval_start >= %s::timestamptz)
  AND (%s::timestamptz IS NULL OR interval_start < %s::timestamptz)
ORDER BY interval_start ASC
"""

_SOC_LIST_SQL = """
SELECT generator_id, interval_start, soc_percent, source_id, source_timestamp,
       ingestion_version, correction_version, quality_flags
FROM generator_soc_5m
WHERE generator_id = %s
  AND (%s::timestamptz IS NULL OR interval_start >= %s::timestamptz)
  AND (%s::timestamptz IS NULL OR interval_start < %s::timestamptz)
ORDER BY interval_start ASC
"""

_PRICE_LIST_SQL = """
SELECT region, interval_start, price_aud_per_mwh, price_status,
       intervention, apc_flag, market_suspended, source_id, source_timestamp,
       ingestion_version, correction_version, quality_flags
FROM nem_price_5m
WHERE region = %s
  AND (%s::timestamptz IS NULL OR interval_start >= %s::timestamptz)
  AND (%s::timestamptz IS NULL OR interval_start < %s::timestamptz)
ORDER BY interval_start ASC
"""


def _generator_from_row(row: tuple[Any, ...]) -> GeneratorMetadata:
    return GeneratorMetadata(
        generator_id=row[0],
        site_name=row[1],
        region=row[2],
        capacity_mw=row[3],
        storage_capacity_mwh=row[4],
        source_id=row[5],
        source_timestamp=row[6],
        ingestion_version=row[7],
        correction_version=row[8],
        data_start=row[9],
        data_end=row[10],
    )


def _power_from_row(row: tuple[Any, ...]) -> GeneratorPower5m:
    return GeneratorPower5m(
        generator_id=row[0],
        interval_start=row[1],
        power_mw=row[2],
        source_id=row[3],
        source_timestamp=row[4],
        ingestion_version=row[5],
        correction_version=row[6],
    )


def _soc_from_row(row: tuple[Any, ...]) -> GeneratorSoc5m:
    return GeneratorSoc5m(
        generator_id=row[0],
        interval_start=row[1],
        soc_percent=row[2],
        source_id=row[3],
        source_timestamp=row[4],
        ingestion_version=row[5],
        correction_version=row[6],
        quality_flags=tuple(row[7] or ()),
    )


def _price_from_row(row: tuple[Any, ...]) -> RegionalPrice5m:
    return RegionalPrice5m(
        region=row[0],
        interval_start=row[1],
        price_aud_per_mwh=row[2],
        price_status=row[3],
        intervention=row[4],
        apc_flag=row[5],
        market_suspended=row[6],
        source_id=row[7],
        source_timestamp=row[8],
        ingestion_version=row[9],
        correction_version=row[10],
        quality_flags=tuple(row[11] or ()),
    )


class PostgreSQLRepository:
    """PostgreSQL implementation of the S2a storage boundary.

    The supplied connection is owned by the caller.  Writes commit one
    transaction each and return ``True`` only when PostgreSQL inserted or
    replaced the effective row; an exact replay or stale revision returns
    ``False`` because the guarded ``ON CONFLICT`` update returns no row.
    """

    def __init__(self, connection: PostgreSQLConnection):
        self._connection = connection

    def _write(self, statement: str, parameters: tuple[Any, ...]) -> bool:
        try:
            with _managed_cursor(self._connection) as cursor:
                cursor.execute(statement, parameters)
                applied = cursor.fetchone() is not None
            self._connection.commit()
            return applied
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise

    def _one(self, statement: str, parameters: tuple[Any, ...]) -> tuple[Any, ...] | None:
        with _managed_cursor(self._connection) as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchone()

    def _many(self, statement: str, parameters: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        with _managed_cursor(self._connection) as cursor:
            cursor.execute(statement, parameters)
            return list(cursor.fetchall())

    def upsert_generator(self, record: GeneratorMetadata) -> bool:
        return self._write(
            _GENERATOR_UPSERT_SQL,
            (
                record.generator_id,
                record.site_name,
                record.region,
                record.capacity_mw,
                record.storage_capacity_mwh,
                record.data_start,
                record.data_end,
                record.source_id,
                record.source_timestamp,
                record.ingestion_version,
                record.correction_version,
            ),
        )

    insert_generator = upsert_generator

    def upsert_power(self, record: GeneratorPower5m) -> bool:
        return self._write(
            _POWER_UPSERT_SQL,
            (
                record.generator_id,
                record.interval_start,
                record.power_mw,
                record.source_id,
                record.source_timestamp,
                record.ingestion_version,
                record.correction_version,
            ),
        )

    insert_power = upsert_power

    def upsert_soc(self, record: GeneratorSoc5m) -> bool:
        return self._write(
            _SOC_UPSERT_SQL,
            (
                record.generator_id,
                record.interval_start,
                record.soc_percent,
                record.source_id,
                record.source_timestamp,
                record.ingestion_version,
                record.correction_version,
                list(record.quality_flags),
            ),
        )

    insert_soc = upsert_soc

    def upsert_price(self, record: RegionalPrice5m) -> bool:
        return self._write(
            _PRICE_UPSERT_SQL,
            (
                record.region,
                record.interval_start,
                record.price_aud_per_mwh,
                record.price_status,
                record.intervention,
                record.apc_flag,
                record.market_suspended,
                record.source_id,
                record.source_timestamp,
                record.ingestion_version,
                record.correction_version,
                list(record.quality_flags),
            ),
        )

    insert_price = upsert_price

    def read_generator(self, generator_id: str) -> GeneratorMetadata | None:
        row = self._one(_GENERATOR_READ_SQL, (_text(generator_id, "generator_id"),))
        return None if row is None else _generator_from_row(row)

    def read_power(self, generator_id: str, interval_start: datetime) -> GeneratorPower5m | None:
        row = self._one(
            _POWER_READ_SQL,
            (_text(generator_id, "generator_id"), aligned_5m(interval_start)),
        )
        return None if row is None else _power_from_row(row)

    def read_soc(self, generator_id: str, interval_start: datetime) -> GeneratorSoc5m | None:
        row = self._one(
            _SOC_READ_SQL,
            (_text(generator_id, "generator_id"), aligned_5m(interval_start)),
        )
        return None if row is None else _soc_from_row(row)

    def read_price(self, region: str, interval_start: datetime) -> RegionalPrice5m | None:
        row = self._one(
            _PRICE_READ_SQL,
            (_text(region, "region"), aligned_5m(interval_start)),
        )
        return None if row is None else _price_from_row(row)

    def list_generators(self) -> tuple[GeneratorMetadata, ...]:
        return tuple(_generator_from_row(row) for row in self._many(_GENERATORS_LIST_SQL, ()))

    @staticmethod
    def _window_parameters(
        dimension: str,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[Any, ...]:
        return (dimension, start, start, end, end)

    def list_power(
        self,
        generator_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[GeneratorPower5m, ...]:
        dimension = _text(generator_id, "generator_id")
        normalized_start, normalized_end = _window(start, end)
        values = [
            _power_from_row(row)
            for row in self._many(
                _POWER_LIST_SQL,
                self._window_parameters(dimension, normalized_start, normalized_end),
            )
        ]
        return tuple(sorted(values, key=lambda record: record.interval_start))

    def list_soc(
        self,
        generator_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[GeneratorSoc5m, ...]:
        dimension = _text(generator_id, "generator_id")
        normalized_start, normalized_end = _window(start, end)
        values = [
            _soc_from_row(row)
            for row in self._many(
                _SOC_LIST_SQL,
                self._window_parameters(dimension, normalized_start, normalized_end),
            )
        ]
        return tuple(sorted(values, key=lambda record: record.interval_start))

    def list_prices(
        self,
        region: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[RegionalPrice5m, ...]:
        dimension = _text(region, "region")
        normalized_start, normalized_end = _window(start, end)
        values = [
            _price_from_row(row)
            for row in self._many(
                _PRICE_LIST_SQL,
                self._window_parameters(dimension, normalized_start, normalized_end),
            )
        ]
        return tuple(sorted(values, key=lambda record: record.interval_start))

    def count_power(self, generator_id: str) -> int:
        return len(self.list_power(generator_id))

    def count_soc(self, generator_id: str) -> int:
        return len(self.list_soc(generator_id))

    def count_prices(self, region: str) -> int:
        return len(self.list_prices(region))

__all__ = [
    "GeneratorMetadata",
    "GeneratorPower5m",
    "GeneratorSoc5m",
    "RegionalPrice5m",
    "RecordProvenance",
    "StorageRepository",
    "Repository",
    "InMemoryRepository",
    "PostgreSQLRepository",
    "PostgreSQLConnection",
    "PostgreSQLCursor",
    "GeneratorRecord",
    "PowerRecord",
    "SocRecord",
    "PriceRecord",
    "PriceStatus",
    "utc_timestamp",
    "aligned_5m",
]
