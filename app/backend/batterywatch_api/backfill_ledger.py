"""Transactional run planning for resumable NEMWeb historical backfills."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from typing import Any, Iterable, Iterator, Protocol


@dataclass(frozen=True, slots=True)
class BackfillRunSpec:
    run_id: str
    requested_start: datetime
    requested_end: datetime
    ingestion_version: int


@dataclass(frozen=True, slots=True)
class BackfillPlanItem:
    feed: str
    report_date: date
    source_url: str


@dataclass(frozen=True, slots=True)
class BackfillEnsureResult:
    created: bool
    resumed: bool
    item_count: int
    recovered_count: int


class BackfillRunConflictError(Exception):
    """Raised when an existing run does not match the requested identity."""


class _Cursor(Protocol):
    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None: ...
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


_RUN_INSERT_SQL = """
INSERT INTO historical_backfill_runs (
    run_id, requested_start, requested_end, ingestion_version, status
)
VALUES (%s, %s, %s, %s, 'running')
ON CONFLICT DO NOTHING RETURNING 1
"""

_RUN_SELECT_SQL = """
SELECT run_id, requested_start, requested_end, ingestion_version
FROM historical_backfill_runs
WHERE run_id = %s
FOR UPDATE
"""

_ITEM_INSERT_SQL = """
INSERT INTO historical_backfill_items (
    run_id, feed, report_date, source_url, status
)
VALUES (%s, %s, %s, %s, 'pending')
"""

_EVENT_INSERT_SQL = """
INSERT INTO historical_backfill_events (
    run_id, feed, report_date, event_type, attempt_number
)
VALUES (%s, %s, %s, %s, %s)
"""

_ITEMS_SELECT_SQL = """
SELECT feed, report_date, source_url
FROM historical_backfill_items
WHERE run_id = %s
ORDER BY feed, report_date
"""

_RECOVER_ITEMS_SQL = """
UPDATE historical_backfill_items
SET status = 'pending', updated_at = CURRENT_TIMESTAMP, started_at = NULL
WHERE run_id = %s AND status = 'running'
RETURNING feed, report_date, attempt_count
"""

_RESUME_RUN_SQL = """
UPDATE historical_backfill_runs
SET status = 'running', updated_at = CURRENT_TIMESTAMP, completed_at = NULL
WHERE run_id = %s
"""

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MAX_BIGINT = (1 << 63) - 1
_FEED_URLS = {
    "dispatch_price": (
        "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/",
        "PUBLIC_DISPATCHIS_",
    ),
    "dispatch_scada": (
        "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/",
        "PUBLIC_DISPATCHSCADA_",
    ),
}


def _validate_spec(spec: BackfillRunSpec) -> None:
    if _RUN_ID_PATTERN.fullmatch(spec.run_id) is None:
        raise ValueError("invalid backfill run id")
    if not isinstance(spec.requested_start, datetime) or not isinstance(
        spec.requested_end, datetime
    ):
        raise ValueError("backfill bounds must be datetimes")
    if spec.requested_start.utcoffset() != timedelta(0) or spec.requested_end.utcoffset() != timedelta(0):
        raise ValueError("backfill bounds must be UTC-aware")
    duration = spec.requested_end - spec.requested_start
    if duration <= timedelta(0) or duration > timedelta(days=366):
        raise ValueError("invalid backfill range")
    if type(spec.ingestion_version) is not int or not 0 <= spec.ingestion_version <= _MAX_BIGINT:
        raise ValueError("invalid ingestion version")


def _validate_items(items: tuple[BackfillPlanItem, ...]) -> None:
    if not items:
        raise ValueError("planned items must not be empty")
    keys: set[tuple[str, date]] = set()
    for item in items:
        if item.feed not in _FEED_URLS or type(item.report_date) is not date:
            raise ValueError("invalid backfill plan item")
        key = (item.feed, item.report_date)
        if key in keys:
            raise ValueError("duplicate backfill plan item")
        keys.add(key)
        base, prefix = _FEED_URLS[item.feed]
        expected_url = f"{base}{prefix}{item.report_date:%Y%m%d}.zip"
        if item.source_url != expected_url:
            raise ValueError("invalid backfill archive URL")


class PostgreSQLBackfillLedger:
    """Own the transaction that creates or exactly resumes a backfill run."""

    def __init__(self, connection: _Connection):
        self._connection = connection

    def ensure_run(
        self,
        spec: BackfillRunSpec,
        planned_items: Iterable[BackfillPlanItem],
    ) -> BackfillEnsureResult:
        materialized = tuple(planned_items)
        _validate_spec(spec)
        _validate_items(materialized)
        items = tuple(sorted(materialized, key=lambda item: (item.feed, item.report_date)))
        try:
            with _managed_cursor(self._connection) as cursor:
                cursor.execute(
                    _RUN_INSERT_SQL,
                    (
                        spec.run_id,
                        spec.requested_start,
                        spec.requested_end,
                        spec.ingestion_version,
                    ),
                )
                if cursor.fetchone() is not None:
                    for item in items:
                        cursor.execute(
                            _ITEM_INSERT_SQL,
                            (spec.run_id, item.feed, item.report_date, item.source_url),
                        )
                        cursor.execute(
                            _EVENT_INSERT_SQL,
                            (spec.run_id, item.feed, item.report_date, "planned", 0),
                        )
                    result = BackfillEnsureResult(True, False, len(items), 0)
                else:
                    cursor.execute(_RUN_SELECT_SQL, (spec.run_id,))
                    stored_run = cursor.fetchone()
                    expected_run = (
                        spec.run_id,
                        spec.requested_start,
                        spec.requested_end,
                        spec.ingestion_version,
                    )
                    if stored_run != expected_run:
                        raise BackfillRunConflictError("backfill run identity conflicts")
                    cursor.execute(_ITEMS_SELECT_SQL, (spec.run_id,))
                    stored_items = tuple(cursor.fetchall())
                    expected_items = tuple(
                        (item.feed, item.report_date, item.source_url) for item in items
                    )
                    if stored_items != expected_items:
                        raise BackfillRunConflictError("backfill plan conflicts")
                    cursor.execute(_RECOVER_ITEMS_SQL, (spec.run_id,))
                    recovered = tuple(cursor.fetchall())
                    for feed, report_date, attempt_number in recovered:
                        cursor.execute(
                            _EVENT_INSERT_SQL,
                            (
                                spec.run_id,
                                feed,
                                report_date,
                                "recovered",
                                attempt_number,
                            ),
                        )
                    cursor.execute(_RESUME_RUN_SQL, (spec.run_id,))
                    result = BackfillEnsureResult(
                        False, True, len(items), len(recovered)
                    )
            self._connection.commit()
            return result
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise


__all__ = [
    "BackfillEnsureResult",
    "BackfillPlanItem",
    "BackfillRunConflictError",
    "BackfillRunSpec",
    "PostgreSQLBackfillLedger",
]
