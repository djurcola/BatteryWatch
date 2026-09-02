"""Immutable registration of nested Next Day daily source artifacts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
import re
from typing import Any, Iterator, Protocol


_MAX_DAILY_ZIP_BYTES = 16 * 1024 * 1024
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_PUBLICATION_RE = re.compile(r"[0-9]{1,32}")
_FILENAME_RE = re.compile(
    r"PUBLIC_NEXT_DAY_DISPATCH_([0-9]{4})([0-9]{2})([0-9]{2})_([0-9]{1,32})\.zip"
)
_BASE_URL = "https://www.nemweb.com.au/REPORTS/ARCHIVE/Next_Day_Dispatch/"


@dataclass(frozen=True, slots=True)
class NestedSourceArtifactReceipt:
    parent_artifact_sha256: str
    report_date: date
    outer_source_url: str
    filename: str
    publication_id: str
    artifact_published_at: datetime
    downloaded_at: datetime
    raw_bytes: bytes


@dataclass(frozen=True, slots=True)
class NestedSourceArtifactResult:
    artifact_sha256: str
    byte_count: int
    replayed: bool


class NestedSourceArtifactConflictError(ValueError):
    """Stored parent or nested artifact conflicts with supplied evidence."""


class _Cursor(Protocol):
    def execute(self, statement: str, parameters: tuple[object, ...]) -> None: ...

    def fetchone(self) -> Any: ...

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


_PARENT_SELECT_SQL = """
SELECT feed, report_date, source_url, parent_artifact_sha256
FROM historical_source_artifacts
WHERE artifact_sha256 = %s
FOR SHARE
"""

_NESTED_INSERT_SQL = """
INSERT INTO historical_source_artifacts (
    artifact_sha256, feed, report_date, source_url, filename,
    byte_count, raw_bytes, parent_artifact_sha256,
    artifact_published_at, artifact_downloaded_at, publication_id
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING RETURNING 1
"""

_NESTED_SELECT_SQL = """
SELECT feed, report_date, source_url, filename, byte_count, raw_bytes,
       parent_artifact_sha256, artifact_published_at,
       artifact_downloaded_at, publication_id
FROM historical_source_artifacts
WHERE artifact_sha256 = %s
"""


def _validate_receipt(receipt: object) -> NestedSourceArtifactReceipt:
    if type(receipt) is not NestedSourceArtifactReceipt:
        raise TypeError("receipt must be NestedSourceArtifactReceipt")
    if (
        type(receipt.parent_artifact_sha256) is not str
        or _SHA_RE.fullmatch(receipt.parent_artifact_sha256) is None
    ):
        raise ValueError("invalid parent artifact SHA")
    if type(receipt.report_date) is not date:
        raise TypeError("report_date must be a date")
    expected_outer_url = (
        f"{_BASE_URL}PUBLIC_NEXT_DAY_DISPATCH_{receipt.report_date:%Y%m}01.zip"
    )
    if receipt.outer_source_url != expected_outer_url:
        raise ValueError("invalid outer source URL")
    if type(receipt.filename) is not str:
        raise TypeError("filename must be a string")
    match = _FILENAME_RE.fullmatch(receipt.filename)
    if match is None:
        raise ValueError("invalid nested artifact filename")
    try:
        filename_date = date(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )
    except (OverflowError, ValueError):
        raise ValueError("invalid nested artifact filename") from None
    if filename_date != receipt.report_date:
        raise ValueError("nested filename does not match report_date")
    if (
        type(receipt.publication_id) is not str
        or _PUBLICATION_RE.fullmatch(receipt.publication_id) is None
        or match.group(4) != receipt.publication_id
    ):
        raise ValueError("invalid publication identity")
    for value, name in (
        (receipt.artifact_published_at, "artifact_published_at"),
        (receipt.downloaded_at, "downloaded_at"),
    ):
        if type(value) is not datetime or value.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be UTC-aware")
    if receipt.artifact_published_at > receipt.downloaded_at:
        raise ValueError("artifact publication cannot follow download")
    if type(receipt.raw_bytes) is not bytes:
        raise TypeError("raw_bytes must be immutable bytes")
    if not 1 <= len(receipt.raw_bytes) <= _MAX_DAILY_ZIP_BYTES:
        raise ValueError("nested artifact size is outside accepted bounds")
    if sha256(receipt.raw_bytes).hexdigest() == receipt.parent_artifact_sha256:
        raise ValueError("nested artifact cannot equal its parent")
    return receipt


class PostgreSQLNestedSourceArtifactRegistrar:
    """Insert one nested daily artifact after verifying its monthly parent."""

    def __init__(self, connection: _Connection):
        self._connection = connection

    def record(
        self, receipt: NestedSourceArtifactReceipt
    ) -> NestedSourceArtifactResult:
        receipt = _validate_receipt(receipt)
        digest = sha256(receipt.raw_bytes).hexdigest()
        byte_count = len(receipt.raw_bytes)
        source_url = f"{receipt.outer_source_url}#{receipt.filename}"
        expected_parent = (
            "nextday_soc",
            date(receipt.report_date.year, receipt.report_date.month, 1),
            receipt.outer_source_url,
            None,
        )
        try:
            with _managed_cursor(self._connection) as cursor:
                cursor.execute(
                    _PARENT_SELECT_SQL,
                    (receipt.parent_artifact_sha256,),
                )
                if cursor.fetchone() != expected_parent:
                    raise NestedSourceArtifactConflictError(
                        "nested artifact parent conflicts"
                    )
                cursor.execute(
                    _NESTED_INSERT_SQL,
                    (
                        digest,
                        "nextday_soc",
                        receipt.report_date,
                        source_url,
                        receipt.filename,
                        byte_count,
                        receipt.raw_bytes,
                        receipt.parent_artifact_sha256,
                        receipt.artifact_published_at,
                        receipt.downloaded_at,
                        receipt.publication_id,
                    ),
                )
                inserted = cursor.fetchone() is not None
                if not inserted:
                    cursor.execute(_NESTED_SELECT_SQL, (digest,))
                    stored = cursor.fetchone()
                    if stored is None or len(stored) != 10:
                        raise NestedSourceArtifactConflictError(
                            "conflicting nested source artifact"
                        )
                    expected = (
                        "nextday_soc",
                        receipt.report_date,
                        source_url,
                        receipt.filename,
                        byte_count,
                        receipt.raw_bytes,
                        receipt.parent_artifact_sha256,
                        receipt.artifact_published_at,
                        receipt.downloaded_at,
                        receipt.publication_id,
                    )
                    normalized = (*stored[:5], bytes(stored[5]), *stored[6:])
                    if normalized != expected:
                        raise NestedSourceArtifactConflictError(
                            "conflicting nested source artifact"
                        )
            self._connection.commit()
            return NestedSourceArtifactResult(digest, byte_count, not inserted)
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise


__all__ = [
    "NestedSourceArtifactConflictError",
    "NestedSourceArtifactReceipt",
    "NestedSourceArtifactResult",
    "PostgreSQLNestedSourceArtifactRegistrar",
]
