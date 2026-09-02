"""Atomic persistence for authoritative individual Next Day SOC."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Iterator, Protocol

from .battery_assets import BatteryAsset
from .nextday_soc import NextDaySocObservation


@dataclass(frozen=True, slots=True)
class NextDaySocIngestionResult:
    source_rows: int
    raw_inserted: int
    raw_replayed: int
    effective_candidates: int
    effective_applied: int
    effective_replayed: int
    source_null_count: int
    percentage_count: int


class NextDaySocConflictError(Exception):
    """Raised when stored SOC evidence conflicts at the same precedence."""


class _Cursor(Protocol):
    rowcount: int

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None: ...
    def executemany(
        self,
        statement: str,
        parameters: Iterable[tuple[Any, ...]],
    ) -> None: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
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


_ARTIFACT_SELECT_SQL = """
SELECT feed
FROM historical_source_artifacts
WHERE artifact_sha256 = %s
FOR SHARE
"""

_RAW_SELECT_SQL = """
SELECT artifact_sha256, generator_id, interval_start, soc_mwh,
       intervention, run_number, dispatch_interval, last_changed,
       report_timestamp, downloaded_at, ingestion_version, correction_version
FROM raw_nextday_soc_observations
WHERE artifact_sha256 = %s
  AND ingestion_version = %s
  AND correction_version = %s
FOR UPDATE
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_nextday_soc_observations (
    artifact_sha256, generator_id, interval_start, soc_mwh,
    intervention, run_number, dispatch_interval, last_changed,
    report_timestamp, downloaded_at, ingestion_version, correction_version
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_EFFECTIVE_SELECT_SQL = """
SELECT generator_id, interval_start, soc_percent,
       source_id, source_timestamp, ingestion_version, correction_version,
       quality_flags, soc_mwh, capacity_mwh, capacity_effective_from,
       capacity_effective_to, capacity_source_id, capacity_source_timestamp,
       report_timestamp, downloaded_at, intervention, run_number,
       dispatch_interval, source_artifact_sha256
FROM generator_soc_5m
WHERE generator_id = ANY(%s)
  AND interval_start >= %s
  AND interval_start <= %s
FOR UPDATE
"""

_EFFECTIVE_UPSERT_SQL = """
INSERT INTO generator_soc_5m (
    generator_id, interval_start, soc_percent,
    source_id, source_timestamp, ingestion_version, correction_version,
    quality_flags, soc_mwh, capacity_mwh, capacity_effective_from,
    capacity_effective_to, capacity_source_id, capacity_source_timestamp,
    report_timestamp, downloaded_at, intervention, run_number,
    dispatch_interval, source_artifact_sha256
)
VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (generator_id, interval_start) DO UPDATE
SET soc_percent = EXCLUDED.soc_percent,
    source_id = EXCLUDED.source_id,
    source_timestamp = EXCLUDED.source_timestamp,
    ingestion_version = EXCLUDED.ingestion_version,
    correction_version = EXCLUDED.correction_version,
    quality_flags = EXCLUDED.quality_flags,
    soc_mwh = EXCLUDED.soc_mwh,
    capacity_mwh = EXCLUDED.capacity_mwh,
    capacity_effective_from = EXCLUDED.capacity_effective_from,
    capacity_effective_to = EXCLUDED.capacity_effective_to,
    capacity_source_id = EXCLUDED.capacity_source_id,
    capacity_source_timestamp = EXCLUDED.capacity_source_timestamp,
    report_timestamp = EXCLUDED.report_timestamp,
    downloaded_at = EXCLUDED.downloaded_at,
    intervention = EXCLUDED.intervention,
    run_number = EXCLUDED.run_number,
    dispatch_interval = EXCLUDED.dispatch_interval,
    source_artifact_sha256 = EXCLUDED.source_artifact_sha256
WHERE generator_soc_5m.source_artifact_sha256 IS NULL
   OR (
    (
    EXCLUDED.intervention,
    EXCLUDED.correction_version,
    EXCLUDED.ingestion_version,
    EXCLUDED.report_timestamp,
    EXCLUDED.source_artifact_sha256
) > (
    generator_soc_5m.intervention,
    generator_soc_5m.correction_version,
    generator_soc_5m.ingestion_version,
    generator_soc_5m.report_timestamp,
    generator_soc_5m.source_artifact_sha256
    )
)
"""


def _raw_parameters(observation: NextDaySocObservation) -> tuple[Any, ...]:
    return (
        observation.source_artifact_id,
        observation.duid,
        observation.interval_start,
        observation.soc_mwh,
        observation.intervention,
        observation.run_number,
        observation.dispatch_interval,
        observation.last_changed,
        observation.report_timestamp,
        observation.downloaded_at,
        observation.ingestion_version,
        observation.correction_version,
    )


def _precedence(observation: NextDaySocObservation) -> tuple[Any, ...]:
    return (
        observation.intervention,
        observation.correction_version,
        observation.ingestion_version,
        observation.report_timestamp,
        observation.source_artifact_id,
    )


def _effective_parameters(
    observation: NextDaySocObservation,
    asset: BatteryAsset,
) -> tuple[Any, ...]:
    quality_flags: list[str]
    capacity_mwh: float | None
    capacity_effective_from: datetime | None
    capacity_source_id: str | None
    capacity_source_timestamp: datetime | None
    soc_percent: float | None
    if observation.soc_mwh is None:
        soc_percent = None
        capacity_mwh = None
        capacity_effective_from = None
        capacity_source_id = None
        capacity_source_timestamp = None
        quality_flags = ["authoritative_soc_missing"]
    elif asset.source_timestamp > observation.interval_start:
        soc_percent = None
        capacity_mwh = None
        capacity_effective_from = None
        capacity_source_id = None
        capacity_source_timestamp = None
        quality_flags = ["capacity_not_effective"]
    elif observation.soc_mwh > asset.storage_capacity_mwh:
        soc_percent = None
        capacity_mwh = None
        capacity_effective_from = None
        capacity_source_id = None
        capacity_source_timestamp = None
        quality_flags = ["soc_exceeds_capacity"]
    else:
        capacity_mwh = asset.storage_capacity_mwh
        capacity_effective_from = asset.source_timestamp
        capacity_source_id = asset.source_id
        capacity_source_timestamp = asset.source_timestamp
        soc_percent = 100.0 * observation.soc_mwh / capacity_mwh
        quality_flags = []
    return (
        observation.duid,
        observation.interval_start,
        soc_percent,
        observation.source_artifact_id,
        observation.last_changed,
        observation.ingestion_version,
        observation.correction_version,
        quality_flags,
        observation.soc_mwh,
        capacity_mwh,
        capacity_effective_from,
        None,
        capacity_source_id,
        capacity_source_timestamp,
        observation.report_timestamp,
        observation.downloaded_at,
        observation.intervention,
        observation.run_number,
        observation.dispatch_interval,
        observation.source_artifact_id,
    )


def _effective_precedence(parameters: tuple[Any, ...]) -> tuple[Any, ...]:
    return (
        parameters[16],
        parameters[6],
        parameters[5],
        parameters[14],
        parameters[19],
    )


def _effective_matches(
    stored: tuple[Any, ...],
    expected: tuple[Any, ...],
) -> bool:
    if len(stored) != len(expected):
        return False
    try:
        stored_flags = tuple(stored[7])
        expected_flags = tuple(expected[7])
    except TypeError:
        return False
    return (
        stored[:7] == expected[:7]
        and stored_flags == expected_flags
        and stored[8:] == expected[8:]
    )


def _validate_inputs(
    observations: tuple[NextDaySocObservation, ...],
    assets: tuple[BatteryAsset, ...],
) -> dict[str, BatteryAsset]:
    if not observations or any(
        not isinstance(item, NextDaySocObservation) for item in observations
    ):
        raise ValueError("invalid Next Day SOC observations")
    if not assets or any(not isinstance(item, BatteryAsset) for item in assets):
        raise ValueError("invalid reviewed battery assets")
    asset_by_duid = {asset.duid: asset for asset in assets}
    if len(asset_by_duid) != len(assets):
        raise ValueError("duplicate reviewed battery asset")
    if any(item.duid not in asset_by_duid for item in observations):
        raise ValueError("unreviewed Next Day SOC DUID")

    first = observations[0]
    if any(
        item.source_artifact_id != first.source_artifact_id
        or item.ingestion_version != first.ingestion_version
        or item.correction_version != first.correction_version
        or item.report_timestamp != first.report_timestamp
        or item.downloaded_at != first.downloaded_at
        for item in observations
    ):
        raise ValueError("mixed Next Day SOC source identity")
    seen: set[tuple[Any, ...]] = set()
    for item in observations:
        key = (
            item.source_artifact_id,
            item.duid,
            item.interval_start,
            item.intervention,
            item.run_number,
            item.ingestion_version,
            item.correction_version,
        )
        if key in seen:
            raise ValueError("duplicate Next Day SOC natural key")
        seen.add(key)
    return asset_by_duid


def _select_effective_candidates(
    observations: tuple[NextDaySocObservation, ...],
) -> tuple[NextDaySocObservation, ...]:
    grouped: dict[tuple[str, datetime], list[NextDaySocObservation]] = {}
    for item in observations:
        grouped.setdefault((item.duid, item.interval_start), []).append(item)
    selected: list[NextDaySocObservation] = []
    for key, candidates in grouped.items():
        winner_precedence = max(_precedence(item) for item in candidates)
        winners = [item for item in candidates if _precedence(item) == winner_precedence]
        winner_payloads = {_raw_parameters(item) for item in winners}
        if len(winner_payloads) != 1:
            raise NextDaySocConflictError(
                f"conflicting effective Next Day SOC candidates for {key[0]}"
            )
        selected.append(winners[0])
    return tuple(sorted(selected, key=lambda item: (item.duid, item.interval_start)))


class PostgreSQLNextDaySocIngestor:
    """Persist one authoritative Next Day SOC artifact transactionally."""

    def __init__(self, connection: _Connection):
        self._connection = connection

    def ingest(
        self,
        observations: Iterable[NextDaySocObservation],
        assets: Iterable[BatteryAsset],
    ) -> NextDaySocIngestionResult:
        try:
            materialized = tuple(observations)
            materialized_assets = tuple(assets)
            asset_by_duid = _validate_inputs(materialized, materialized_assets)
            effective_candidates = _select_effective_candidates(materialized)
            first = materialized[0]

            with _managed_cursor(self._connection) as cursor:
                cursor.execute(_ARTIFACT_SELECT_SQL, (first.source_artifact_id,))
                artifact = cursor.fetchone()
                if artifact != ("nextday_soc",):
                    raise ValueError("artifact is not registered as nextday_soc")

                cursor.execute(
                    _RAW_SELECT_SQL,
                    (
                        first.source_artifact_id,
                        first.ingestion_version,
                        first.correction_version,
                    ),
                )
                stored_raw = {
                    (
                        row[0],
                        row[1],
                        row[2],
                        row[4],
                        row[5],
                        row[10],
                        row[11],
                    ): tuple(row)
                    for row in cursor.fetchall()
                }
                raw_missing: list[tuple[Any, ...]] = []
                raw_replayed = 0
                for observation in materialized:
                    parameters = _raw_parameters(observation)
                    key = (
                        parameters[0],
                        parameters[1],
                        parameters[2],
                        parameters[4],
                        parameters[5],
                        parameters[10],
                        parameters[11],
                    )
                    stored = stored_raw.get(key)
                    if stored is None:
                        raw_missing.append(parameters)
                    elif stored == parameters:
                        raw_replayed += 1
                    else:
                        raise NextDaySocConflictError(
                            "stored raw Next Day SOC conflicts with source"
                        )

                duids = sorted({item.duid for item in effective_candidates})
                intervals = [item.interval_start for item in effective_candidates]
                cursor.execute(
                    _EFFECTIVE_SELECT_SQL,
                    (duids, min(intervals), max(intervals)),
                )
                stored_effective = {
                    (row[0], row[1]): tuple(row) for row in cursor.fetchall()
                }

                effective_apply: list[tuple[Any, ...]] = []
                effective_replayed = 0
                percentage_count = 0
                for observation in effective_candidates:
                    parameters = _effective_parameters(
                        observation,
                        asset_by_duid[observation.duid],
                    )
                    if parameters[2] is not None:
                        percentage_count += 1
                    stored = stored_effective.get((parameters[0], parameters[1]))
                    if stored is None:
                        effective_apply.append(parameters)
                        continue
                    if stored[19] is None:
                        effective_apply.append(parameters)
                        continue
                    candidate_precedence = _effective_precedence(parameters)
                    stored_precedence = _effective_precedence(stored)
                    if candidate_precedence > stored_precedence:
                        effective_apply.append(parameters)
                    elif candidate_precedence == stored_precedence:
                        if not _effective_matches(stored, parameters):
                            raise NextDaySocConflictError(
                                "stored effective Next Day SOC conflicts at equal precedence"
                            )
                        effective_replayed += 1
                    else:
                        effective_replayed += 1

                if raw_missing:
                    cursor.executemany(_RAW_INSERT_SQL, raw_missing)
                    raw_inserted = cursor.rowcount
                    if raw_inserted != len(raw_missing):
                        raise NextDaySocConflictError(
                            "raw Next Day SOC bulk write was incomplete"
                        )
                else:
                    raw_inserted = 0
                if effective_apply:
                    cursor.executemany(_EFFECTIVE_UPSERT_SQL, effective_apply)
                    effective_applied = cursor.rowcount
                    if not 0 <= effective_applied <= len(effective_apply):
                        raise NextDaySocConflictError(
                            "invalid effective Next Day SOC bulk result"
                        )
                    if effective_applied < len(effective_apply):
                        cursor.execute(
                            _EFFECTIVE_SELECT_SQL,
                            (duids, min(intervals), max(intervals)),
                        )
                        final_effective = {
                            (row[0], row[1]): tuple(row) for row in cursor.fetchall()
                        }
                        for parameters in effective_apply:
                            stored = final_effective.get((parameters[0], parameters[1]))
                            if stored is None or stored[19] is None:
                                raise NextDaySocConflictError(
                                    "guarded effective Next Day SOC write is missing"
                                )
                            candidate_precedence = _effective_precedence(parameters)
                            stored_precedence = _effective_precedence(stored)
                            if stored_precedence < candidate_precedence or (
                                stored_precedence == candidate_precedence
                                and not _effective_matches(stored, parameters)
                            ):
                                raise NextDaySocConflictError(
                                    "guarded effective Next Day SOC write conflicts"
                                )
                    effective_replayed += len(effective_apply) - effective_applied
                else:
                    effective_applied = 0

            self._connection.commit()
            return NextDaySocIngestionResult(
                source_rows=len(materialized),
                raw_inserted=raw_inserted,
                raw_replayed=raw_replayed,
                effective_candidates=len(effective_candidates),
                effective_applied=effective_applied,
                effective_replayed=effective_replayed,
                source_null_count=sum(item.soc_mwh is None for item in materialized),
                percentage_count=percentage_count,
            )
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise


__all__ = [
    "NextDaySocConflictError",
    "NextDaySocIngestionResult",
    "PostgreSQLNextDaySocIngestor",
]
