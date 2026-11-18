"""Transactional durable state for individual NEMWeb CURRENT artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Protocol, cast


CURRENT_FEEDS = ("dispatch_scada", "dispatch_price")
CURRENT_ARTIFACTS_PER_FEED_LIMIT = 12
MAX_CURRENT_ERROR_SUMMARY_LENGTH = 512
_SOURCE_ID_RE = re.compile(r"[0-9]{1,32}")
_NEM_TIMEZONE = timezone(timedelta(hours=10))
_SCADA_FILENAME_RE = re.compile(
    r"PUBLIC_DISPATCHSCADA_(?P<timestamp>[0-9]{12})_(?P<source_id>[0-9]{1,32})\.zip"
)
_PRICE_FILENAME_RE = re.compile(
    r"PUBLIC_DISPATCHIS_(?P<timestamp>[0-9]{12})_(?P<source_id>[0-9]{1,32})\.zip"
)


@dataclass(frozen=True, slots=True)
class CurrentArtifactReference:
    """Immutable identity discovered in a CURRENT directory listing."""

    feed: str
    source_artifact_id: str
    source_url: str
    zip_filename: str
    report_timestamp: datetime

    @property
    def filename(self) -> str:
        return self.zip_filename


@dataclass(frozen=True, slots=True)
class CurrentArtifactLedgerReconciliation:
    discovered: int
    inserted: int

    @property
    def new(self) -> int:
        return self.inserted


@dataclass(frozen=True, slots=True)
class CurrentArtifactCompletion:
    replayed: bool


@dataclass(frozen=True, slots=True)
class CurrentArtifactMiss:
    error_summary: str


@dataclass(frozen=True, slots=True)
class CurrentArtifactClaim:
    reference: CurrentArtifactReference
    attempt_number: int

    @property
    def feed(self) -> str:
        return self.reference.feed

    @property
    def source_artifact_id(self) -> str:
        return self.reference.source_artifact_id

    @property
    def source_url(self) -> str:
        return self.reference.source_url

    @property
    def zip_filename(self) -> str:
        return self.reference.zip_filename

    @property
    def report_timestamp(self) -> datetime:
        return self.reference.report_timestamp


class CurrentArtifactLedgerConflictError(Exception):
    """Raised when a CURRENT identity changes after it was recorded."""


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


def _coerce_reference(feed: str, reference: Any) -> CurrentArtifactReference:
    if type(reference) is CurrentArtifactReference:
        if reference.feed != feed:
            raise ValueError("CURRENT artifact feed does not match reconciliation feed")
        return reference
    try:
        return CurrentArtifactReference(
            feed=feed,
            source_artifact_id=reference.source_artifact_id,
            source_url=reference.url,
            zip_filename=reference.zip_filename,
            report_timestamp=reference.report_timestamp,
        )
    except AttributeError:
        raise ValueError("invalid CURRENT artifact reference") from None


def _validate_reference(reference: CurrentArtifactReference) -> None:
    if type(reference) is not CurrentArtifactReference:
        raise ValueError("invalid CURRENT artifact reference")
    if reference.feed not in CURRENT_FEEDS:
        raise ValueError("invalid CURRENT artifact feed")
    if (
        type(reference.source_artifact_id) is not str
        or _SOURCE_ID_RE.fullmatch(reference.source_artifact_id) is None
    ):
        raise ValueError("invalid CURRENT artifact source id")
    if type(reference.zip_filename) is not str or "/" in reference.zip_filename:
        raise ValueError("invalid CURRENT artifact filename")
    if reference.feed == "dispatch_scada":
        prefix = "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
        match = _SCADA_FILENAME_RE.fullmatch(reference.zip_filename)
    else:
        prefix = "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
        match = _PRICE_FILENAME_RE.fullmatch(reference.zip_filename)
    if match is None or match.group("source_id") != reference.source_artifact_id:
        raise ValueError("invalid CURRENT artifact filename")
    if reference.source_url != prefix + reference.zip_filename:
        raise ValueError("invalid CURRENT artifact URL")
    try:
        expected_timestamp = datetime.strptime(
            match.group("timestamp"), "%Y%m%d%H%M"
        ).replace(tzinfo=_NEM_TIMEZONE).astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError("invalid CURRENT artifact timestamp") from None
    if (
        type(reference.report_timestamp) is not datetime
        or reference.report_timestamp.tzinfo is None
        or reference.report_timestamp.utcoffset() is None
        or reference.report_timestamp != expected_timestamp
    ):
        raise ValueError("invalid CURRENT artifact timestamp")


_RECONCILE_INSERT_SQL = """
INSERT INTO current_artifact_ledger (
    feed, source_artifact_id, source_url, zip_filename, report_timestamp, status
)
VALUES (%s, %s, %s, %s, %s, 'pending')
ON CONFLICT (feed, source_artifact_id) DO NOTHING RETURNING 1
"""

_RECONCILE_SELECT_SQL = """
SELECT source_url, zip_filename, report_timestamp, status, attempt_count
FROM current_artifact_ledger
WHERE feed = %s AND source_artifact_id = %s
FOR UPDATE
"""

_RECONCILE_SEEN_SQL = """
UPDATE current_artifact_ledger
SET last_seen_at = CURRENT_TIMESTAMP
WHERE feed = %s AND source_artifact_id = %s
"""

_CLAIM_SELECT_SQL = """
WITH eligible AS (
    SELECT ledger.feed, ledger.source_artifact_id, ledger.source_url,
           ledger.zip_filename, ledger.report_timestamp, ledger.status,
           ledger.attempt_count
    FROM current_artifact_ledger AS ledger
    WHERE ledger.feed = %s AND ledger.status IN ('pending', 'missed')
      AND (
          ledger.last_attempted_at IS NULL
          OR ledger.last_attempted_at <= CURRENT_TIMESTAMP - INTERVAL '15 minutes'
      )
), oldest AS (
    SELECT feed, source_artifact_id, source_url, zip_filename,
           report_timestamp
    FROM eligible
    ORDER BY report_timestamp ASC, source_artifact_id::numeric ASC, zip_filename ASC
    LIMIT %s
), newest AS (
    SELECT feed, source_artifact_id, source_url, zip_filename,
           report_timestamp
    FROM eligible
    ORDER BY report_timestamp DESC, source_artifact_id::numeric DESC, zip_filename DESC
    LIMIT %s
), selected AS (
    SELECT feed, source_artifact_id, source_url, zip_filename,
           report_timestamp
    FROM oldest
    UNION
    SELECT feed, source_artifact_id, source_url, zip_filename,
           report_timestamp
    FROM newest
)
SELECT ledger.feed, ledger.source_artifact_id, ledger.source_url,
       ledger.zip_filename, ledger.report_timestamp, ledger.status,
       ledger.attempt_count
FROM current_artifact_ledger AS ledger
JOIN selected
  ON selected.feed = ledger.feed
 AND selected.source_artifact_id = ledger.source_artifact_id
 AND selected.source_url = ledger.source_url
 AND selected.zip_filename = ledger.zip_filename
 AND selected.report_timestamp = ledger.report_timestamp
WHERE ledger.status IN ('pending', 'missed')
  AND (
      ledger.last_attempted_at IS NULL
      OR ledger.last_attempted_at <= CURRENT_TIMESTAMP - INTERVAL '15 minutes'
  )
ORDER BY ledger.report_timestamp ASC,
         ledger.source_artifact_id::numeric ASC,
         ledger.zip_filename ASC
FOR UPDATE OF ledger SKIP LOCKED
"""

_CLAIM_UPDATE_SQL = """
UPDATE current_artifact_ledger
SET attempt_count = attempt_count + 1,
    last_attempted_at = CURRENT_TIMESTAMP
WHERE feed = %s AND source_artifact_id = %s
  AND status IN ('pending', 'missed') AND attempt_count = %s
RETURNING attempt_count
"""

_ITEM_SELECT_SQL = """
SELECT source_url, zip_filename, report_timestamp, status, attempt_count
FROM current_artifact_ledger
WHERE feed = %s AND source_artifact_id = %s
FOR UPDATE
"""

_COMPLETE_UPDATE_SQL = """
UPDATE current_artifact_ledger
SET status = 'completed', completed_at = CURRENT_TIMESTAMP, last_error = NULL
WHERE feed = %s AND source_artifact_id = %s
  AND status IN ('pending', 'missed') AND attempt_count = %s
RETURNING 1
"""

_MISS_UPDATE_SQL = """
UPDATE current_artifact_ledger
SET status = 'missed', last_error = %s, completed_at = NULL,
    last_attempted_at = COALESCE(last_attempted_at, CURRENT_TIMESTAMP)
WHERE feed = %s AND source_artifact_id = %s
  AND status IN ('pending', 'missed') AND attempt_count = %s
RETURNING 1
"""


def _reference_parameters(reference: CurrentArtifactReference) -> tuple[Any, ...]:
    return (
        reference.feed,
        reference.source_artifact_id,
        reference.source_url,
        reference.zip_filename,
        reference.report_timestamp,
    )


def bounded_error_summary(error_summary: str) -> str:
    """Return a short, control-character-free summary safe for ledger storage."""

    if type(error_summary) is not str:
        raise ValueError("invalid CURRENT artifact error summary")
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", error_summary).strip()
    cleaned = re.sub(r"https?://\S+", "<url>", cleaned)
    cleaned = re.sub(
        r"(?i)(password|secret|token|authorization|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        cleaned,
    )
    if not cleaned:
        raise ValueError("invalid CURRENT artifact error summary")
    return cleaned[:MAX_CURRENT_ERROR_SUMMARY_LENGTH].rstrip()


def _validate_claim(claim: CurrentArtifactClaim) -> None:
    if type(claim) is not CurrentArtifactClaim:
        raise ValueError("invalid CURRENT artifact claim")
    _validate_reference(claim.reference)
    if (
        type(claim.attempt_number) is not int
        or not 1 <= claim.attempt_number <= (1 << 63) - 1
    ):
        raise ValueError("invalid CURRENT artifact attempt number")


class PostgreSQLCurrentArtifactLedger:
    """Persist and reconcile individual CURRENT references."""

    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def complete(self, claim: CurrentArtifactClaim) -> CurrentArtifactCompletion:
        _validate_claim(claim)
        try:
            with _managed_cursor(self._connection) as cursor:
                cursor.execute(
                    _ITEM_SELECT_SQL,
                    (claim.feed, claim.source_artifact_id),
                )
                current = cursor.fetchone()
                expected_identity = (
                    claim.source_url,
                    claim.zip_filename,
                    claim.report_timestamp,
                )
                if current is None or len(current) != 5:
                    raise CurrentArtifactLedgerConflictError(
                        "CURRENT ledger claim does not exist"
                    )
                if tuple(current[:3]) != expected_identity:
                    raise CurrentArtifactLedgerConflictError(
                        "CURRENT ledger identity conflicts with stored identity"
                    )
                if current[3] == "completed":
                    result = CurrentArtifactCompletion(True)
                else:
                    if (
                        current[3] not in {"pending", "missed"}
                        or current[4] != claim.attempt_number
                    ):
                        raise CurrentArtifactLedgerConflictError(
                            "CURRENT ledger claim is stale"
                        )
                    cursor.execute(
                        _COMPLETE_UPDATE_SQL,
                        (
                            claim.feed,
                            claim.source_artifact_id,
                            claim.attempt_number,
                        ),
                    )
                    if cursor.fetchone() is None:
                        raise CurrentArtifactLedgerConflictError(
                            "CURRENT ledger completion was not applied"
                        )
                    result = CurrentArtifactCompletion(False)
            self._connection.commit()
            return result
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise

    def miss(
        self,
        claim: CurrentArtifactClaim,
        *,
        error_summary: str,
    ) -> CurrentArtifactMiss:
        _validate_claim(claim)
        bounded = bounded_error_summary(error_summary)
        try:
            with _managed_cursor(self._connection) as cursor:
                cursor.execute(
                    _ITEM_SELECT_SQL,
                    (claim.feed, claim.source_artifact_id),
                )
                current = cursor.fetchone()
                expected_identity = (
                    claim.source_url,
                    claim.zip_filename,
                    claim.report_timestamp,
                )
                if current is None or len(current) != 5:
                    raise CurrentArtifactLedgerConflictError(
                        "CURRENT ledger claim does not exist"
                    )
                if tuple(current[:3]) != expected_identity:
                    raise CurrentArtifactLedgerConflictError(
                        "CURRENT ledger identity conflicts with stored identity"
                    )
                if (
                    current[3] not in {"pending", "missed"}
                    or current[4] != claim.attempt_number
                ):
                    raise CurrentArtifactLedgerConflictError(
                        "CURRENT ledger claim is stale"
                    )
                cursor.execute(
                    _MISS_UPDATE_SQL,
                    (
                        bounded,
                        claim.feed,
                        claim.source_artifact_id,
                        claim.attempt_number,
                    ),
                )
                if cursor.fetchone() is None:
                    raise CurrentArtifactLedgerConflictError(
                        "CURRENT ledger miss was not applied"
                    )
            self._connection.commit()
            return CurrentArtifactMiss(bounded)
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise

    def claim_pending(
        self,
        feed: str,
        *,
        limit: int = CURRENT_ARTIFACTS_PER_FEED_LIMIT,
    ) -> tuple[CurrentArtifactClaim, ...]:
        if feed not in CURRENT_FEEDS:
            raise ValueError("invalid CURRENT artifact feed")
        if type(limit) is not int or not 0 < limit <= CURRENT_ARTIFACTS_PER_FEED_LIMIT:
            raise ValueError("invalid CURRENT artifact limit")
        try:
            claims: list[CurrentArtifactClaim] = []
            with _managed_cursor(self._connection) as cursor:
                oldest_limit = limit // 2
                newest_limit = limit - oldest_limit
                cursor.execute(
                    _CLAIM_SELECT_SQL,
                    (feed, oldest_limit, newest_limit),
                )
                rows = cursor.fetchall()
                for row in rows:
                    if len(row) != 7:
                        raise CurrentArtifactLedgerConflictError(
                            "invalid CURRENT ledger selection row"
                        )
                    reference = CurrentArtifactReference(*row[:5])
                    _validate_reference(reference)
                    if row[5] not in {"pending", "missed"}:
                        raise CurrentArtifactLedgerConflictError(
                            "invalid CURRENT ledger selection status"
                        )
                    if type(row[6]) is not int or row[6] < 0:
                        raise CurrentArtifactLedgerConflictError(
                            "invalid CURRENT ledger attempt count"
                        )
                    cursor.execute(
                        _CLAIM_UPDATE_SQL,
                        (feed, reference.source_artifact_id, row[6]),
                    )
                    updated = cursor.fetchone()
                    if updated is None or len(updated) != 1:
                        raise CurrentArtifactLedgerConflictError(
                            "CURRENT ledger claim was not applied"
                        )
                    attempt_number = updated[0]
                    if type(attempt_number) is not int or attempt_number <= 0:
                        raise CurrentArtifactLedgerConflictError(
                            "invalid CURRENT ledger attempt number"
                        )
                    claims.append(CurrentArtifactClaim(reference, attempt_number))
            self._connection.commit()
            return tuple(claims)
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise

    def claim_next(self, feed: str) -> CurrentArtifactClaim | None:
        claims = self.claim_pending(feed, limit=1)
        return claims[0] if claims else None

    def reconcile(
        self,
        references: Iterable[CurrentArtifactReference] | str,
        source_references: Iterable[Any] | None = None,
    ) -> CurrentArtifactLedgerReconciliation:
        if source_references is None:
            materialized: tuple[CurrentArtifactReference, ...] = tuple(
                cast(Iterable[CurrentArtifactReference], references)
            )
        else:
            if type(references) is not str or references not in CURRENT_FEEDS:
                raise ValueError("invalid CURRENT artifact feed")
            materialized = tuple(
                _coerce_reference(references, reference)
                for reference in source_references
            )
        for reference in materialized:
            _validate_reference(reference)
        keys = {(item.feed, item.source_artifact_id) for item in materialized}
        if len(keys) != len(materialized):
            raise ValueError("duplicate CURRENT artifact reference")
        try:
            inserted = 0
            with _managed_cursor(self._connection) as cursor:
                for reference in materialized:
                    cursor.execute(
                        _RECONCILE_INSERT_SQL,
                        _reference_parameters(reference),
                    )
                    if cursor.fetchone() is not None:
                        inserted += 1
                        continue
                    cursor.execute(
                        _RECONCILE_SELECT_SQL,
                        (reference.feed, reference.source_artifact_id),
                    )
                    stored = cursor.fetchone()
                    if stored is None or len(stored) != 5:
                        raise CurrentArtifactLedgerConflictError(
                            "CURRENT ledger reference disappeared"
                        )
                    if tuple(stored[:3]) != (
                        reference.source_url,
                        reference.zip_filename,
                        reference.report_timestamp,
                    ):
                        raise CurrentArtifactLedgerConflictError(
                            "CURRENT ledger identity conflicts with stored identity"
                        )
                    cursor.execute(
                        _RECONCILE_SEEN_SQL,
                        (reference.feed, reference.source_artifact_id),
                    )
            self._connection.commit()
            return CurrentArtifactLedgerReconciliation(len(materialized), inserted)
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise


PostgreSQLCollectorLedger = PostgreSQLCurrentArtifactLedger


__all__ = [
    "CURRENT_ARTIFACTS_PER_FEED_LIMIT",
    "CURRENT_FEEDS",
    "CurrentArtifactClaim",
    "CurrentArtifactCompletion",
    "CurrentArtifactLedgerConflictError",
    "CurrentArtifactMiss",
    "CurrentArtifactLedgerReconciliation",
    "CurrentArtifactReference",
    "MAX_CURRENT_ERROR_SUMMARY_LENGTH",
    "bounded_error_summary",
    "PostgreSQLCollectorLedger",
    "PostgreSQLCurrentArtifactLedger",
]
