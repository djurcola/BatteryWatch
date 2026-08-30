"""Immutable historical archive registration through a narrow public seam."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol

from .backfill_ledger import BackfillClaim


class _Cursor(Protocol):
    def execute(self, statement: str, parameters: tuple[object, ...]) -> None: ...

    def fetchone(self) -> Any: ...

    def close(self) -> None: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...

    def commit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackfillArtifactReceipt:
    """Raw archive evidence downloaded for a claimed backfill item."""

    claim: BackfillClaim
    downloaded_at: datetime
    source_last_modified: datetime | None
    raw_archive: bytes


@dataclass(frozen=True, slots=True)
class BackfillArtifactResult:
    """Identity of a persisted archive and whether its attempt link was replayed."""

    artifact_sha256: str
    byte_count: int
    replayed: bool


_ITEM_LOCK_SQL = """
SELECT source_url, status, attempt_count
FROM historical_backfill_items
WHERE run_id = %s
  AND feed = %s
  AND report_date = %s
FOR UPDATE
"""

_ARTIFACT_INSERT_SQL = """
INSERT INTO historical_source_artifacts (
    artifact_sha256,
    feed,
    report_date,
    source_url,
    filename,
    byte_count,
    raw_archive
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING RETURNING 1
"""

_LINK_INSERT_SQL = """
INSERT INTO historical_backfill_item_artifacts (
    run_id,
    feed,
    report_date,
    attempt_number,
    artifact_sha256,
    downloaded_at,
    source_last_modified
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING RETURNING 1
"""

_EVENT_INSERT_SQL = """
INSERT INTO historical_backfill_events (
    run_id,
    feed,
    report_date,
    event_type,
    attempt_number
)
VALUES (%s, %s, %s, %s, %s)
"""


class PostgreSQLBackfillArtifactRegistrar:
    """Persist first-seen archive evidence for one live backfill claim."""

    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def record(self, receipt: BackfillArtifactReceipt) -> BackfillArtifactResult:
        claim = receipt.claim
        digest = sha256(receipt.raw_archive).hexdigest()
        byte_count = len(receipt.raw_archive)
        filename = claim.source_url.rsplit("/", 1)[-1]
        cursor = self._connection.cursor()
        try:
            cursor.execute(
                _ITEM_LOCK_SQL,
                (claim.run_id, claim.feed, claim.report_date),
            )
            cursor.fetchone()
            cursor.execute(
                _ARTIFACT_INSERT_SQL,
                (
                    digest,
                    claim.feed,
                    claim.report_date,
                    claim.source_url,
                    filename,
                    byte_count,
                    receipt.raw_archive,
                ),
            )
            cursor.fetchone()
            cursor.execute(
                _LINK_INSERT_SQL,
                (
                    claim.run_id,
                    claim.feed,
                    claim.report_date,
                    claim.attempt_number,
                    digest,
                    receipt.downloaded_at,
                    receipt.source_last_modified,
                ),
            )
            cursor.fetchone()
            cursor.execute(
                _EVENT_INSERT_SQL,
                (
                    claim.run_id,
                    claim.feed,
                    claim.report_date,
                    "artifact_recorded",
                    claim.attempt_number,
                ),
            )
            self._connection.commit()
            return BackfillArtifactResult(digest, byte_count, False)
        finally:
            cursor.close()
