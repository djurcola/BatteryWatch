"""Atomic persistence for grouped FCAS evidence from retained Next Day SOC files."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Iterator, Protocol

from psycopg.types.json import Jsonb

from .nextday_soc import NextDaySocObservation


@dataclass(frozen=True, slots=True)
class NextDayFcasIngestionResult:
    """Counts for one grouped FCAS projection ingestion."""

    source_rows: int
    raw_inserted: int
    raw_replayed: int
    effective_candidates: int
    effective_applied: int
    effective_replayed: int
    reported_service_count: int

    @property
    def fcas_raw_inserted(self) -> int:
        return self.raw_inserted

    @property
    def fcas_raw_replayed(self) -> int:
        return self.raw_replayed

    @property
    def fcas_effective_candidates(self) -> int:
        return self.effective_candidates

    @property
    def fcas_effective_applied(self) -> int:
        return self.effective_applied

    @property
    def fcas_effective_replayed(self) -> int:
        return self.effective_replayed

    @property
    def fcas_reported_service_count(self) -> int:
        return self.reported_service_count


class NextDayFcasConflictError(Exception):
    """Raised when stored FCAS evidence conflicts at the same precedence."""


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
SELECT artifact_sha256, generator_id, interval_start, fcas_services,
       intervention, run_number, dispatch_interval, last_changed,
       report_timestamp, downloaded_at, ingestion_version, correction_version
FROM raw_nextday_fcas_observations
WHERE artifact_sha256 = %s
  AND ingestion_version = %s
  AND correction_version = %s
FOR UPDATE
"""

_RAW_INSERT_SQL = """
INSERT INTO raw_nextday_fcas_observations (
    artifact_sha256, generator_id, interval_start, fcas_services,
    intervention, run_number, dispatch_interval, last_changed,
    report_timestamp, downloaded_at, ingestion_version, correction_version
)
VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_EFFECTIVE_SELECT_SQL = """
SELECT generator_id, interval_start, fcas_services, last_changed,
       report_timestamp, downloaded_at, intervention, run_number,
       dispatch_interval, ingestion_version, correction_version,
       source_artifact_sha256
FROM generator_fcas_5m
WHERE generator_id = ANY(%s)
  AND interval_start >= %s
  AND interval_start <= %s
FOR UPDATE
"""

_EFFECTIVE_UPSERT_SQL = """
INSERT INTO generator_fcas_5m (
    generator_id, interval_start, fcas_services, last_changed,
    report_timestamp, downloaded_at, intervention, run_number,
    dispatch_interval, ingestion_version, correction_version,
    source_artifact_sha256
)
VALUES (
    %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (generator_id, interval_start) DO UPDATE
SET fcas_services = EXCLUDED.fcas_services,
    last_changed = EXCLUDED.last_changed,
    report_timestamp = EXCLUDED.report_timestamp,
    downloaded_at = EXCLUDED.downloaded_at,
    intervention = EXCLUDED.intervention,
    run_number = EXCLUDED.run_number,
    dispatch_interval = EXCLUDED.dispatch_interval,
    ingestion_version = EXCLUDED.ingestion_version,
    correction_version = EXCLUDED.correction_version,
    source_artifact_sha256 = EXCLUDED.source_artifact_sha256
WHERE generator_fcas_5m.source_artifact_sha256 IS NULL
   OR (
    (
    EXCLUDED.intervention,
    EXCLUDED.correction_version,
    EXCLUDED.ingestion_version,
    EXCLUDED.report_timestamp,
    EXCLUDED.source_artifact_sha256
) > (
    generator_fcas_5m.intervention,
    generator_fcas_5m.correction_version,
    generator_fcas_5m.ingestion_version,
    generator_fcas_5m.report_timestamp,
    generator_fcas_5m.source_artifact_sha256
    )
)
"""


def _fcas_map(
    observation: NextDaySocObservation,
) -> dict[str, dict[str, float | int | None]]:
    return observation.fcas.as_dict()


def _jsonb_parameter(
    observation: NextDaySocObservation,
) -> Jsonb:
    # Jsonb is required by psycopg for adapting a Python mapping to JSONB.  The
    # SQL cast also keeps the storage contract explicit for alternate drivers.
    return Jsonb(_fcas_map(observation))


def _raw_parameters(observation: NextDaySocObservation) -> tuple[Any, ...]:
    return (
        observation.source_artifact_id,
        observation.duid,
        observation.interval_start,
        _jsonb_parameter(observation),
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


def _json_data(value: Any) -> Any:
    if isinstance(value, Jsonb):
        return value.obj
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


def _raw_matches(stored: tuple[Any, ...], expected: tuple[Any, ...]) -> bool:
    if len(stored) != len(expected):
        return False
    return (
        stored[:3] == expected[:3]
        and _json_data(stored[3]) == _json_data(expected[3])
        and stored[4:] == expected[4:]
    )


def _raw_identity(observation: NextDaySocObservation) -> tuple[Any, ...]:
    return (
        observation.source_artifact_id,
        observation.duid,
        observation.interval_start,
        observation.intervention,
        observation.run_number,
        observation.dispatch_interval,
        observation.last_changed,
        observation.report_timestamp,
        observation.downloaded_at,
        observation.ingestion_version,
        observation.correction_version,
        json.dumps(_fcas_map(observation), sort_keys=True, separators=(",", ":")),
    )


def _effective_parameters(
    observation: NextDaySocObservation,
) -> tuple[Any, ...]:
    return (
        observation.duid,
        observation.interval_start,
        _jsonb_parameter(observation),
        observation.last_changed,
        observation.report_timestamp,
        observation.downloaded_at,
        observation.intervention,
        observation.run_number,
        observation.dispatch_interval,
        observation.ingestion_version,
        observation.correction_version,
        observation.source_artifact_id,
    )


def _effective_precedence(parameters: tuple[Any, ...]) -> tuple[Any, ...]:
    return (
        parameters[6],
        parameters[10],
        parameters[9],
        parameters[4],
        parameters[11],
    )


def _effective_matches(
    stored: tuple[Any, ...],
    expected: tuple[Any, ...],
) -> bool:
    if len(stored) != len(expected):
        return False
    return (
        stored[:2] == expected[:2]
        and _json_data(stored[2]) == _json_data(expected[2])
        and stored[3:] == expected[3:]
    )


def _validate_inputs(
    observations: tuple[NextDaySocObservation, ...],
) -> None:
    if not observations or any(
        not isinstance(item, NextDaySocObservation) for item in observations
    ):
        raise ValueError("invalid Next Day FCAS observations")

    first = observations[0]
    if any(
        item.source_artifact_id != first.source_artifact_id
        or item.ingestion_version != first.ingestion_version
        or item.correction_version != first.correction_version
        or item.report_timestamp != first.report_timestamp
        or item.downloaded_at != first.downloaded_at
        for item in observations
    ):
        raise ValueError("mixed Next Day FCAS source identity")

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
            raise ValueError("duplicate Next Day FCAS natural key")
        seen.add(key)


def _select_effective_candidates(
    observations: tuple[NextDaySocObservation, ...],
) -> tuple[NextDaySocObservation, ...]:
    grouped: dict[tuple[str, datetime], list[NextDaySocObservation]] = {}
    for item in observations:
        grouped.setdefault((item.duid, item.interval_start), []).append(item)

    selected: list[NextDaySocObservation] = []
    for key, candidates in grouped.items():
        winner_precedence = max(_precedence(item) for item in candidates)
        winners = [
            item for item in candidates
            if _precedence(item) == winner_precedence
        ]
        winner_payloads = {_raw_identity(item) for item in winners}
        if len(winner_payloads) != 1:
            raise NextDayFcasConflictError(
                f"conflicting effective Next Day FCAS candidates for {key[0]}"
            )
        selected.append(winners[0])
    return tuple(sorted(selected, key=lambda item: (item.duid, item.interval_start)))


class PostgreSQLNextDayFcasIngestor:
    """Persist grouped FCAS evidence without registering a second artifact."""

    def __init__(self, connection: _Connection):
        self._connection = connection

    def ingest(
        self,
        observations: Iterable[NextDaySocObservation],
    ) -> NextDayFcasIngestionResult:
        try:
            materialized = tuple(observations)
            _validate_inputs(materialized)
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
                    elif _raw_matches(stored, parameters):
                        raw_replayed += 1
                    else:
                        raise NextDayFcasConflictError(
                            "stored raw Next Day FCAS conflicts with source"
                        )

                duids = sorted({item.duid for item in effective_candidates})
                intervals = [item.interval_start for item in effective_candidates]
                cursor.execute(
                    _EFFECTIVE_SELECT_SQL,
                    (duids, min(intervals), max(intervals)),
                )
                stored_effective = {
                    (row[0], row[1]): tuple(row)
                    for row in cursor.fetchall()
                }

                effective_apply: list[tuple[Any, ...]] = []
                effective_replayed = 0
                for observation in effective_candidates:
                    parameters = _effective_parameters(observation)
                    stored = stored_effective.get((parameters[0], parameters[1]))
                    if stored is None:
                        effective_apply.append(parameters)
                        continue
                    if stored[11] is None:
                        effective_apply.append(parameters)
                        continue
                    candidate_precedence = _effective_precedence(parameters)
                    stored_precedence = _effective_precedence(stored)
                    if candidate_precedence > stored_precedence:
                        effective_apply.append(parameters)
                    elif candidate_precedence == stored_precedence:
                        if not _effective_matches(stored, parameters):
                            raise NextDayFcasConflictError(
                                "stored effective Next Day FCAS conflicts at equal precedence"
                            )
                        effective_replayed += 1
                    else:
                        effective_replayed += 1

                if raw_missing:
                    cursor.executemany(_RAW_INSERT_SQL, raw_missing)
                    raw_inserted = cursor.rowcount
                    if raw_inserted != len(raw_missing):
                        raise NextDayFcasConflictError(
                            "raw Next Day FCAS bulk write was incomplete"
                        )
                else:
                    raw_inserted = 0

                if effective_apply:
                    cursor.executemany(_EFFECTIVE_UPSERT_SQL, effective_apply)
                    effective_applied = cursor.rowcount
                    if not 0 <= effective_applied <= len(effective_apply):
                        raise NextDayFcasConflictError(
                            "invalid effective Next Day FCAS bulk result"
                        )
                    if effective_applied < len(effective_apply):
                        cursor.execute(
                            _EFFECTIVE_SELECT_SQL,
                            (duids, min(intervals), max(intervals)),
                        )
                        final_effective = {
                            (row[0], row[1]): tuple(row)
                            for row in cursor.fetchall()
                        }
                        for parameters in effective_apply:
                            stored = final_effective.get(
                                (parameters[0], parameters[1])
                            )
                            if stored is None or stored[11] is None:
                                raise NextDayFcasConflictError(
                                    "guarded effective Next Day FCAS write is missing"
                                )
                            candidate_precedence = _effective_precedence(parameters)
                            stored_precedence = _effective_precedence(stored)
                            if stored_precedence < candidate_precedence or (
                                stored_precedence == candidate_precedence
                                and not _effective_matches(stored, parameters)
                            ):
                                raise NextDayFcasConflictError(
                                    "guarded effective Next Day FCAS write conflicts"
                                )
                    effective_replayed += len(effective_apply) - effective_applied
                else:
                    effective_applied = 0

            self._connection.commit()
            return NextDayFcasIngestionResult(
                source_rows=len(materialized),
                raw_inserted=raw_inserted,
                raw_replayed=raw_replayed,
                effective_candidates=len(effective_candidates),
                effective_applied=effective_applied,
                effective_replayed=effective_replayed,
                reported_service_count=sum(
                    item.fcas.reported_service_count for item in materialized
                ),
            )
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise


__all__ = [
    "NextDayFcasConflictError",
    "NextDayFcasIngestionResult",
    "PostgreSQLNextDayFcasIngestor",
]
