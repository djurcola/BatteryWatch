"""Atomic persistence for verified raw Dispatch SCADA observations."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator, Protocol


@dataclass(frozen=True, slots=True)
class DispatchScadaIngestionResult:
    raw_observation_count: int
    mapped_power_count: int
    replayed: bool


class DispatchScadaConflictError(Exception):
    """Raised when an artifact identity is already bound to different evidence."""


@dataclass(frozen=True, slots=True)
class DispatchScadaArtifactReceipt:
    source_artifact_id: str
    source_url: str
    zip_filename: str
    csv_member_name: str
    report_timestamp: datetime
    zip_sha256: str
    raw_zip: bytes


@dataclass(frozen=True, slots=True)
class RawDispatchScadaObservation:
    source_artifact_id: str
    duid: str
    interval_start: datetime
    power_mw: float
    source_timestamp: datetime
    ingestion_version: int = 0
    correction_version: int = 0


class _Cursor(Protocol):
    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def close(self) -> None: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


@contextmanager
def _managed_cursor(connection: _Connection) -> Iterator[_Cursor]:
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


_ARTIFACT_INSERT_SQL = """
INSERT INTO dispatch_scada_artifacts (
    source_artifact_id, source_url, zip_filename, csv_member_name,
    report_timestamp, zip_sha256, raw_zip
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING RETURNING 1
"""

_ARTIFACT_SELECT_SQL = """
SELECT source_artifact_id, source_url, zip_filename, csv_member_name,
       report_timestamp, zip_sha256, raw_zip
FROM dispatch_scada_artifacts
WHERE source_artifact_id = %s
"""

_OBSERVATION_INSERT_SQL = """
INSERT INTO raw_dispatch_scada_observations (
    source_artifact_id, duid, interval_start, power_mw, source_timestamp,
    ingestion_version, correction_version
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

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
       generator_power_5m.ingestion_version, generator_power_5m.source_timestamp)
RETURNING 1
"""

_GENERATOR_BOUNDS_UPDATE_SQL = """
UPDATE generators
SET data_start = LEAST(COALESCE(generators.data_start, bounds.data_start), bounds.data_start),
    data_end = GREATEST(COALESCE(generators.data_end, bounds.data_end), bounds.data_end),
    updated_at = CURRENT_TIMESTAMP
FROM (VALUES (%s, %s, %s)) AS bounds(data_start, data_end, generator_id)
WHERE generators.generator_id = bounds.generator_id
"""


def _record_value(record: Any, *names: str) -> Any:
    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return record[name]
    for name in names:
        try:
            return getattr(record, name)
        except AttributeError:
            pass
    raise AttributeError(f"record has none of: {', '.join(names)}")


def _generator_id(record: Any) -> str:
    return _record_value(record, "generator_id", "duid")


def _power_key(record: Any) -> tuple[Any, Any]:
    return (
        _generator_id(record),
        _record_value(record, "interval_start", "timestamp"),
    )


def _receipt_parameters(receipt: DispatchScadaArtifactReceipt) -> tuple[Any, ...]:
    return (
        receipt.source_artifact_id,
        receipt.source_url,
        receipt.zip_filename,
        receipt.csv_member_name,
        receipt.report_timestamp,
        receipt.zip_sha256,
        receipt.raw_zip,
    )


def _normalized_bytes(value: Any) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    return bytes(value)


def _receipt_matches(
    receipt: DispatchScadaArtifactReceipt, stored: tuple[Any, ...]
) -> bool:
    if len(stored) != 7:
        return False
    expected = _receipt_parameters(receipt)
    return (
        expected[0] == stored[0]
        and expected[2:6] == stored[2:6]
        and _normalized_bytes(expected[6]) == _normalized_bytes(stored[6])
    )


class PostgreSQLDispatchScadaIngestor:
    def __init__(self, connection: _Connection):
        self._connection = connection

    def ingest(
        self,
        receipt: DispatchScadaArtifactReceipt,
        observations: Iterable[RawDispatchScadaObservation],
        generators: Iterable[Any] = (),
        power_records: Iterable[Any] = (),
    ) -> DispatchScadaIngestionResult:
        try:
            materialized = tuple(observations)
            if not materialized:
                raise ValueError("observations must not be empty")
            for observation in materialized:
                if observation.source_artifact_id != receipt.source_artifact_id:
                    raise ValueError("observation source artifact does not match receipt")

            generator_rows = tuple(generators)
            generator_by_id: dict[Any, Any] = {}
            for generator in generator_rows:
                generator_id = _generator_id(generator)
                if generator_id in generator_by_id:
                    raise ValueError("duplicate generator metadata key")
                generator_by_id[generator_id] = generator

            power_rows = tuple(power_records)
            power_keys: set[tuple[Any, Any]] = set()
            power_by_generator: dict[Any, list[Any]] = {}
            for power in power_rows:
                key = _power_key(power)
                if key in power_keys:
                    raise ValueError("duplicate generator power key")
                power_keys.add(key)
                if key[0] not in generator_by_id:
                    raise ValueError("power record generator is not in metadata")
                power_by_generator.setdefault(key[0], []).append(power)

            replayed = False
            with _managed_cursor(self._connection) as cursor:
                cursor.execute(_ARTIFACT_INSERT_SQL, _receipt_parameters(receipt))
                if cursor.fetchone() is None:
                    cursor.execute(
                        _ARTIFACT_SELECT_SQL, (receipt.source_artifact_id,)
                    )
                    stored = cursor.fetchone()
                    if stored is None or not _receipt_matches(receipt, stored):
                        raise DispatchScadaConflictError(
                            "dispatch SCADA artifact conflicts with stored evidence"
                        )
                    replayed = True
                else:
                    for observation in materialized:
                        cursor.execute(
                            _OBSERVATION_INSERT_SQL,
                            (
                                observation.source_artifact_id,
                                observation.duid,
                                observation.interval_start,
                                observation.power_mw,
                                observation.source_timestamp,
                                observation.ingestion_version,
                                observation.correction_version,
                            ),
                        )

                    for generator in generator_rows:
                        generator_id = _generator_id(generator)
                        cursor.execute(
                            _GENERATOR_UPSERT_SQL,
                            (
                                generator_id,
                                _record_value(generator, "site_name"),
                                _record_value(generator, "region"),
                                _record_value(generator, "capacity_mw"),
                                _record_value(generator, "storage_capacity_mwh"),
                                _record_value(generator, "data_start"),
                                _record_value(generator, "data_end"),
                                _record_value(generator, "source_id"),
                                _record_value(generator, "source_timestamp"),
                                _record_value(generator, "ingestion_version"),
                                _record_value(generator, "correction_version"),
                            ),
                        )
                        cursor.fetchone()

                    for power in power_rows:
                        cursor.execute(
                            _POWER_UPSERT_SQL,
                            (
                                _generator_id(power),
                                _record_value(power, "interval_start", "timestamp"),
                                _record_value(power, "power_mw"),
                                _record_value(power, "source_id"),
                                _record_value(power, "source_timestamp"),
                                _record_value(power, "ingestion_version"),
                                _record_value(power, "correction_version"),
                            ),
                        )
                        cursor.fetchone()

                    for generator_id, generator_power_rows in power_by_generator.items():
                        interval_starts = tuple(
                            _record_value(power, "interval_start", "timestamp")
                            for power in generator_power_rows
                        )
                        cursor.execute(
                            _GENERATOR_BOUNDS_UPDATE_SQL,
                            (
                                min(interval_starts),
                                max(interval_starts) + timedelta(minutes=5),
                                generator_id,
                            ),
                        )
            self._connection.commit()
            if replayed:
                return DispatchScadaIngestionResult(0, 0, True)
            return DispatchScadaIngestionResult(
                len(materialized), len(power_rows), False
            )
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise


__all__ = [
    "DispatchScadaArtifactReceipt",
    "DispatchScadaConflictError",
    "DispatchScadaIngestionResult",
    "PostgreSQLDispatchScadaIngestor",
    "RawDispatchScadaObservation",
]
