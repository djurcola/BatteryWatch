"""Immutable historical archive registration through a narrow public seam."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
import re
from typing import Any, Protocol

from .backfill_ledger import BackfillClaim


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ATTEMPT_NUMBER = 2_147_483_647
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_FEED_URL_PREFIXES = {
    "dispatch_scada": (
        "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
        "PUBLIC_DISPATCHSCADA_"
    ),
    "dispatch_price": (
        "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
        "PUBLIC_DISPATCHIS_"
    ),
}


class _Cursor(Protocol):
    def execute(self, statement: str, parameters: tuple[object, ...]) -> None: ...

    def fetchone(self) -> Any: ...

    def close(self) -> None: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class BackfillArtifactConflictError(ValueError):
    """Stored artifact evidence conflicts with the current receipt."""


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


def _validate_receipt(receipt: object) -> BackfillArtifactReceipt:
    if type(receipt) is not BackfillArtifactReceipt:
        raise TypeError("receipt must be BackfillArtifactReceipt")
    claim = receipt.claim
    if type(claim) is not BackfillClaim:
        raise TypeError("receipt claim must be BackfillClaim")
    if not isinstance(claim.run_id, str) or _RUN_ID_RE.fullmatch(claim.run_id) is None:
        raise ValueError("invalid run_id")
    prefix = _FEED_URL_PREFIXES.get(claim.feed)
    if prefix is None:
        raise ValueError("invalid feed")
    if type(claim.report_date) is not date:
        raise TypeError("report_date must be a date")
    expected_url = f"{prefix}{claim.report_date:%Y%m%d}.zip"
    if claim.source_url != expected_url:
        raise ValueError("source_url does not match feed and report_date")
    if (
        isinstance(claim.attempt_number, bool)
        or not isinstance(claim.attempt_number, int)
        or not 1 <= claim.attempt_number <= _MAX_ATTEMPT_NUMBER
    ):
        raise ValueError("invalid attempt_number")
    if (
        type(receipt.downloaded_at) is not datetime
        or receipt.downloaded_at.utcoffset() != timedelta(0)
    ):
        raise ValueError("downloaded_at must be UTC-aware")
    if receipt.source_last_modified is not None:
        if (
            type(receipt.source_last_modified) is not datetime
            or receipt.source_last_modified.utcoffset() != timedelta(0)
        ):
            raise ValueError("source_last_modified must be UTC-aware")
        if receipt.source_last_modified > receipt.downloaded_at:
            raise ValueError("source_last_modified cannot be after downloaded_at")
    if type(receipt.raw_archive) is not bytes:
        raise TypeError("raw_archive must be immutable bytes")
    if not 1 <= len(receipt.raw_archive) <= MAX_ARCHIVE_BYTES:
        raise ValueError("raw_archive size is outside the accepted bounds")
    return receipt


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
    raw_bytes
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING RETURNING 1
"""

_ARTIFACT_SELECT_SQL = """
SELECT feed, report_date, source_url, filename, byte_count, raw_bytes
FROM historical_source_artifacts
WHERE artifact_sha256 = %s
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

_LINK_SELECT_SQL = """
SELECT artifact_sha256, downloaded_at, source_last_modified
FROM historical_backfill_item_artifacts
WHERE run_id = %s
  AND feed = %s
  AND report_date = %s
  AND attempt_number = %s
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
        receipt = _validate_receipt(receipt)
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
            current_item = cursor.fetchone()
            expected_item = (claim.source_url, "running", claim.attempt_number)
            if current_item != expected_item:
                raise BackfillArtifactConflictError(
                    "receipt does not match the current backfill claim"
                )
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
            artifact_inserted = cursor.fetchone() is not None
            if not artifact_inserted:
                cursor.execute(_ARTIFACT_SELECT_SQL, (digest,))
                stored_artifact = cursor.fetchone()
                expected_artifact = (
                    claim.feed,
                    claim.report_date,
                    claim.source_url,
                    filename,
                    byte_count,
                    receipt.raw_archive,
                )
                if stored_artifact is None:
                    raise BackfillArtifactConflictError(
                        "conflicting historical source artifact"
                    )
                normalized_artifact = (*stored_artifact[:-1], bytes(stored_artifact[-1]))
                if normalized_artifact != expected_artifact:
                    raise BackfillArtifactConflictError(
                        "conflicting historical source artifact"
                    )
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
            link_inserted = cursor.fetchone() is not None
            if not link_inserted:
                cursor.execute(
                    _LINK_SELECT_SQL,
                    (claim.run_id, claim.feed, claim.report_date, claim.attempt_number),
                )
                stored_link = cursor.fetchone()
                expected_link = (
                    digest,
                    receipt.downloaded_at,
                    receipt.source_last_modified,
                )
                if stored_link != expected_link:
                    raise BackfillArtifactConflictError(
                        "conflicting historical backfill artifact link"
                    )
            else:
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
            return BackfillArtifactResult(digest, byte_count, not link_inserted)
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise
        finally:
            cursor.close()
