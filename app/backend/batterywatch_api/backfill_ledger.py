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
class BackfillRunProgress:
    run_id: str
    status: str
    total: int
    pending: int
    running: int
    completed: int
    failed: int
    total_attempts: int


@dataclass(frozen=True, slots=True)
class BackfillPlanItem:
    feed: str
    report_date: date
    source_url: str


@dataclass(frozen=True, slots=True)
class BackfillClaim:
    run_id: str
    feed: str
    report_date: date
    source_url: str
    attempt_number: int


@dataclass(frozen=True, slots=True)
class BackfillItemCompletion:
    replayed: bool
    records_imported: int


@dataclass(frozen=True, slots=True)
class BackfillItemFailure:
    replayed: bool
    error_summary: str


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

_RUN_PROGRESS_SQL = """
SELECT
    r.status,
    COUNT(i.feed),
    COUNT(*) FILTER (WHERE i.status = 'pending'),
    COUNT(*) FILTER (WHERE i.status = 'running'),
    COUNT(*) FILTER (WHERE i.status = 'completed'),
    COUNT(*) FILTER (WHERE i.status = 'failed'),
    COALESCE(SUM(i.attempt_count), 0)::bigint
FROM historical_backfill_runs AS r
LEFT JOIN historical_backfill_items AS i ON i.run_id = r.run_id
WHERE r.run_id = %s
GROUP BY r.status
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

_CLAIM_SELECT_SQL = """
SELECT feed, report_date, source_url
FROM historical_backfill_items
WHERE run_id = %s AND status IN ('pending', 'failed')
ORDER BY feed, report_date
LIMIT 1
FOR UPDATE SKIP LOCKED
"""

_CLAIM_RUN_SELECT_SQL = """
SELECT status
FROM historical_backfill_runs
WHERE run_id = %s
"""

_CLAIM_UPDATE_SQL = """
UPDATE historical_backfill_items
SET status = 'running', attempt_count = attempt_count + 1,
    started_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
WHERE run_id = %s AND feed = %s AND report_date = %s
  AND status IN ('pending', 'failed')
RETURNING feed, report_date, source_url, attempt_count
"""

_COMPLETE_LOCK_SQL = """
SELECT source_url, status, attempt_count
FROM historical_backfill_items
WHERE run_id = %s AND feed = %s AND report_date = %s
FOR UPDATE
"""

_COMPLETE_UPDATE_SQL = """
UPDATE historical_backfill_items
SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP, last_error = NULL
WHERE run_id = %s AND feed = %s AND report_date = %s
  AND status = 'running' AND attempt_count = %s
RETURNING 1
"""

_COMPLETE_EVENT_SQL = """
INSERT INTO historical_backfill_events (
    run_id, feed, report_date, event_type, attempt_number, details
)
VALUES (
    %s, %s, %s, %s, %s,
    jsonb_build_object('records_imported', %s)
)
"""

_FAIL_UPDATE_SQL = """
UPDATE historical_backfill_items
SET status = 'failed', completed_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP, last_error = %s
WHERE run_id = %s AND feed = %s AND report_date = %s
  AND status = 'running' AND attempt_count = %s
RETURNING 1
"""

_FAIL_EVENT_SQL = """
INSERT INTO historical_backfill_events (
    run_id, feed, report_date, event_type, attempt_number, details
)
VALUES (
    %s, %s, %s, %s, %s,
    jsonb_build_object('error_summary', %s)
)
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


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("invalid backfill run id")


def _validate_spec(spec: BackfillRunSpec) -> None:
    _validate_run_id(spec.run_id)
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


def _validate_claim(claim: BackfillClaim) -> None:
    if type(claim) is not BackfillClaim:
        raise ValueError("invalid backfill claim")
    _validate_run_id(claim.run_id)
    _validate_items(
        (BackfillPlanItem(claim.feed, claim.report_date, claim.source_url),)
    )
    if (
        type(claim.attempt_number) is not int
        or not 1 <= claim.attempt_number <= _MAX_BIGINT
    ):
        raise ValueError("invalid backfill attempt number")


def _progress_from_row(
    run_id: str, row: tuple[Any, ...]
) -> BackfillRunProgress:
    if len(row) != 7 or row[0] not in {"running", "completed", "failed"}:
        raise BackfillRunConflictError("invalid backfill progress row")
    counts = row[1:]
    if any(
        type(value) is not int or not 0 <= value <= _MAX_BIGINT
        for value in counts
    ):
        raise BackfillRunConflictError("invalid backfill progress counts")
    total, pending, running, completed, failed, total_attempts = counts
    if (
        total <= 0
        or pending + running + completed + failed != total
        or total_attempts < running + completed + failed
    ):
        raise BackfillRunConflictError("inconsistent backfill progress counts")
    return BackfillRunProgress(run_id, row[0], *counts)


class PostgreSQLBackfillLedger:
    """Own the transaction that creates or exactly resumes a backfill run."""

    def __init__(self, connection: _Connection):
        self._connection = connection

    def progress(self, run_id: str) -> BackfillRunProgress:
        _validate_run_id(run_id)
        try:
            with _managed_cursor(self._connection) as cursor:
                cursor.execute(_RUN_PROGRESS_SQL, (run_id,))
                row = cursor.fetchone()
                if row is None:
                    raise BackfillRunConflictError("backfill run does not exist")
                result = _progress_from_row(run_id, row)
            self._connection.commit()
            return result
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise

    def fail(
        self, claim: BackfillClaim, *, error_summary: str
    ) -> BackfillItemFailure:
        _validate_claim(claim)
        if (
            type(error_summary) is not str
            or not 1 <= len(error_summary) <= 2048
            or error_summary != error_summary.strip()
            or any(character in "\r\n\0" for character in error_summary)
        ):
            raise ValueError("invalid backfill error summary")
        try:
            with _managed_cursor(self._connection) as cursor:
                item_key = (claim.run_id, claim.feed, claim.report_date)
                cursor.execute(_COMPLETE_LOCK_SQL, item_key)
                current = cursor.fetchone()
                expected = (
                    claim.source_url,
                    "running",
                    claim.attempt_number,
                )
                if current != expected:
                    raise BackfillRunConflictError(
                        "backfill claim no longer owns the running item"
                    )
                cursor.execute(
                    _FAIL_UPDATE_SQL,
                    (error_summary, *item_key, claim.attempt_number),
                )
                if cursor.fetchone() is None:
                    raise BackfillRunConflictError(
                        "backfill item failure was not applied"
                    )
                cursor.execute(
                    _FAIL_EVENT_SQL,
                    (
                        *item_key,
                        "failed",
                        claim.attempt_number,
                        error_summary,
                    ),
                )
            self._connection.commit()
            return BackfillItemFailure(False, error_summary)
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise

    def complete(
        self, claim: BackfillClaim, *, records_imported: int
    ) -> BackfillItemCompletion:
        _validate_claim(claim)
        if (
            type(records_imported) is not int
            or not 0 <= records_imported <= _MAX_BIGINT
        ):
            raise ValueError("invalid imported record count")
        try:
            with _managed_cursor(self._connection) as cursor:
                item_key = (claim.run_id, claim.feed, claim.report_date)
                cursor.execute(_COMPLETE_LOCK_SQL, item_key)
                current = cursor.fetchone()
                expected = (
                    claim.source_url,
                    "running",
                    claim.attempt_number,
                )
                if current != expected:
                    raise BackfillRunConflictError(
                        "backfill claim no longer owns the running item"
                    )
                cursor.execute(
                    _COMPLETE_UPDATE_SQL,
                    (*item_key, claim.attempt_number),
                )
                if cursor.fetchone() is None:
                    raise BackfillRunConflictError(
                        "backfill item completion was not applied"
                    )
                cursor.execute(
                    _COMPLETE_EVENT_SQL,
                    (
                        *item_key,
                        "completed",
                        claim.attempt_number,
                        records_imported,
                    ),
                )
            self._connection.commit()
            return BackfillItemCompletion(False, records_imported)
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise

    def claim_next(self, run_id: str) -> BackfillClaim | None:
        _validate_run_id(run_id)
        try:
            with _managed_cursor(self._connection) as cursor:
                cursor.execute(_CLAIM_RUN_SELECT_SQL, (run_id,))
                run_row = cursor.fetchone()
                if run_row is None:
                    raise BackfillRunConflictError("backfill run does not exist")
                if run_row not in {("running",), ("completed",), ("failed",)}:
                    raise BackfillRunConflictError("invalid backfill run status")
                cursor.execute(_CLAIM_SELECT_SQL, (run_id,))
                selected = cursor.fetchone()
                if selected is None:
                    result = None
                else:
                    feed, report_date, source_url = selected
                    cursor.execute(
                        _CLAIM_UPDATE_SQL,
                        (run_id, feed, report_date),
                    )
                    claimed = cursor.fetchone()
                    if claimed is None:
                        raise RuntimeError("claimed backfill item disappeared")
                    claimed_feed, claimed_date, claimed_url, attempt_number = claimed
                    cursor.execute(
                        _EVENT_INSERT_SQL,
                        (
                            run_id,
                            claimed_feed,
                            claimed_date,
                            "claimed",
                            attempt_number,
                        ),
                    )
                    result = BackfillClaim(
                        run_id,
                        claimed_feed,
                        claimed_date,
                        claimed_url,
                        attempt_number,
                    )
            self._connection.commit()
            return result
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise

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
    "BackfillClaim",
    "BackfillEnsureResult",
    "BackfillItemCompletion",
    "BackfillItemFailure",
    "BackfillPlanItem",
    "BackfillRunConflictError",
    "BackfillRunProgress",
    "BackfillRunSpec",
    "PostgreSQLBackfillLedger",
]
