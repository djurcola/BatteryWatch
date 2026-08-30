"""Atomic persistence for official NEMWeb DispatchIS regional prices."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Iterator, Protocol

from .storage import RegionalPrice5m


_REGIONS = {"NSW1", "QLD1", "SA1", "TAS1", "VIC1"}
_MAX_BIGINT = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class DispatchPriceArtifactReceipt:
    source_artifact_id: str
    source_url: str
    zip_filename: str
    csv_member_name: str
    report_timestamp: datetime
    zip_sha256: str
    raw_zip: bytes


@dataclass(frozen=True, slots=True)
class DispatchPriceIngestionResult:
    price_count: int
    replayed: bool


class DispatchPriceArtifactConflictError(Exception):
    """Raised when an artifact identity is already bound to different evidence."""


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
INSERT INTO dispatch_price_artifacts (
    source_artifact_id, source_url, zip_filename, csv_member_name,
    report_timestamp, zip_sha256, raw_zip
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING RETURNING 1
"""

_ARTIFACT_SELECT_SQL = """
SELECT source_artifact_id, source_url, zip_filename, csv_member_name,
       report_timestamp, zip_sha256, raw_zip
FROM dispatch_price_artifacts
WHERE source_artifact_id = %s
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
       nem_price_5m.ingestion_version, nem_price_5m.source_timestamp)
RETURNING 1
"""


def _receipt_parameters(receipt: DispatchPriceArtifactReceipt) -> tuple[Any, ...]:
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
    receipt: DispatchPriceArtifactReceipt,
    stored: tuple[Any, ...],
) -> bool:
    if len(stored) != 7:
        return False
    expected = _receipt_parameters(receipt)
    return expected[:6] == stored[:6] and _normalized_bytes(expected[6]) == _normalized_bytes(
        stored[6]
    )


def _price_parameters(record: RegionalPrice5m) -> tuple[Any, ...]:
    return (
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
    )


class PostgreSQLDispatchPriceIngestor:
    """Persist one canonical five-region DispatchIS artifact transactionally."""

    def __init__(self, connection: _Connection):
        self._connection = connection

    def ingest(
        self,
        receipt: DispatchPriceArtifactReceipt,
        records: Iterable[RegionalPrice5m],
    ) -> DispatchPriceIngestionResult:
        try:
            materialized = tuple(records)
            regions = {record.region for record in materialized}
            intervals = {record.interval_start for record in materialized}
            if len(materialized) != 5 or regions != _REGIONS or len(intervals) != 1:
                raise ValueError(
                    "price artifact must contain exactly the five canonical regions for one interval"
                )
            if any(record.source_id != receipt.source_artifact_id for record in materialized):
                raise ValueError("price source artifact does not match receipt")
            if intervals != {receipt.report_timestamp}:
                raise ValueError("price interval does not match artifact report timestamp")
            try:
                artifact_version = int(receipt.source_artifact_id)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("invalid price artifact version") from None
            if not 0 <= artifact_version <= _MAX_BIGINT or any(
                record.ingestion_version != artifact_version
                for record in materialized
            ):
                raise ValueError("price ingestion version does not match artifact version")

            applied = 0
            replayed = False
            with _managed_cursor(self._connection) as cursor:
                cursor.execute(_ARTIFACT_INSERT_SQL, _receipt_parameters(receipt))
                if cursor.fetchone() is None:
                    cursor.execute(
                        _ARTIFACT_SELECT_SQL,
                        (receipt.source_artifact_id,),
                    )
                    stored = cursor.fetchone()
                    if stored is None or not _receipt_matches(receipt, stored):
                        raise DispatchPriceArtifactConflictError(
                            "DispatchIS price artifact conflicts with stored evidence"
                        )
                    replayed = True
                else:
                    for record in materialized:
                        cursor.execute(_PRICE_UPSERT_SQL, _price_parameters(record))
                        if cursor.fetchone() is not None:
                            applied += 1

            self._connection.commit()
            return DispatchPriceIngestionResult(0 if replayed else applied, replayed)
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise


__all__ = [
    "DispatchPriceArtifactConflictError",
    "DispatchPriceArtifactReceipt",
    "DispatchPriceIngestionResult",
    "PostgreSQLDispatchPriceIngestor",
]
