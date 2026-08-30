"""One bounded historical DispatchIS regional-price backfill claim."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from importlib import import_module
from typing import Any

from .aemo import parse_dispatch_price_mms_csv
from .backfill_artifacts import (
    BackfillArtifactReceipt,
    BackfillArtifactResult,
    PostgreSQLBackfillArtifactRegistrar,
)
from .backfill_ledger import (
    BackfillClaim,
    BackfillItemCompletion,
    PostgreSQLBackfillLedger,
)
from .collector_service import run_price_collection_cycle
from .dispatch_price_ingestion import DispatchPriceIngestionResult
from .nemweb_archives import (
    ARCHIVE_SOURCE,
    DISPATCHIS_PRICE_FEED,
    MAX_OUTER_ARCHIVE_BYTES,
    ArchivePlanItem,
    NemwebArchiveExtraction,
    extract_nested_archive,
)
from .nemweb_dispatch_prices import (
    DISPATCH_PRICE_INDEX_URL,
    DispatchPriceArtifact,
    DispatchPriceArtifactRef,
    DispatchPriceCollection,
    extract_dispatch_price_zip,
)
from .nemweb_http import NemwebHttpResource, fetch_nemweb_resource

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class HistoricalPriceBackfillResult:
    interval_artifact_count: int
    price_count: int
    applied_price_count: int
    replayed_interval_count: int
    outer_artifact_replayed: bool
    completion_replayed: bool


def _connect(database_url: str, *, connect_timeout: int) -> Any:
    psycopg = import_module("psycopg")
    return psycopg.connect(database_url, connect_timeout=connect_timeout)


@contextmanager
def _connection(database_url: str, connect: Callable[..., Any]):
    connection = connect(database_url, connect_timeout=10)
    try:
        yield connection
    finally:
        connection.close()


def _validated_range(
    database_url: str,
    claim: BackfillClaim,
    start: datetime,
    end: datetime,
) -> tuple[datetime, datetime]:
    if not isinstance(database_url, str) or not database_url:
        raise ValueError("database URL is required")
    if type(claim) is not BackfillClaim or claim.feed != "dispatch_price":
        raise ValueError("dispatch price backfill claim is required")
    if (
        not isinstance(start, datetime)
        or not isinstance(end, datetime)
        or start.tzinfo is None
        or start.utcoffset() is None
        or end.tzinfo is None
        or end.utcoffset() is None
    ):
        raise ValueError("backfill range must be timezone-aware")
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    if start_utc >= end_utc:
        raise ValueError("backfill start must precede end")
    return start_utc, end_utc


def _last_modified(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("invalid NEMWeb Last-Modified header") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid NEMWeb Last-Modified header")
    return parsed.astimezone(UTC)


def _execute_price_claim(
    database_url: str,
    claim: BackfillClaim,
    start: datetime,
    end: datetime,
    *,
    connect: Callable[..., Any],
    fetch: Callable[..., NemwebHttpResource],
    clock: Callable[[], datetime],
    ledger_factory: Callable[[Any], Any],
    registrar_factory: Callable[[Any], Any],
    extract_archive: Callable[..., NemwebArchiveExtraction],
    extract_zip: Callable[[DispatchPriceArtifactRef, bytes], DispatchPriceArtifact],
    parse_csv: Callable[..., tuple[Any, ...]],
    ingest_cycle: Callable[..., DispatchPriceIngestionResult],
) -> HistoricalPriceBackfillResult:
    start_utc, end_utc = _validated_range(database_url, claim, start, end)
    resource = fetch(claim.source_url, max_bytes=MAX_OUTER_ARCHIVE_BYTES)
    downloaded_at = clock()
    if downloaded_at.tzinfo is None or downloaded_at.utcoffset() is None:
        raise ValueError("download clock must be timezone-aware")
    downloaded_at = downloaded_at.astimezone(UTC)

    outer_receipt = BackfillArtifactReceipt(
        claim,
        downloaded_at,
        _last_modified(resource.last_modified),
        resource.body,
    )
    with _connection(database_url, connect) as connection:
        artifact_result: BackfillArtifactResult = registrar_factory(connection).record(
            outer_receipt
        )

    extraction = extract_archive(
        ArchivePlanItem(
            DISPATCHIS_PRICE_FEED,
            claim.report_date,
            ARCHIVE_SOURCE,
            claim.source_url,
        ),
        resource.body,
        outer_url=claim.source_url,
        start=start_utc,
        end=end_utc,
    )
    if not extraction.nested:
        raise ValueError("daily DispatchIS archive selected no intervals")

    price_count = 0
    applied_price_count = 0
    replayed_intervals = 0
    for nested in extraction.nested:
        reference = DispatchPriceArtifactRef(
            url=DISPATCH_PRICE_INDEX_URL + nested.member_name,
            zip_filename=nested.member_name,
            source_artifact_id=nested.source_artifact_id,
            report_timestamp=nested.interval_timestamp,
        )
        artifact = extract_zip(reference, nested.raw_bytes)
        records = tuple(
            parse_csv(
                artifact.csv_payload,
                source_id=reference.source_artifact_id,
                ingestion_version=0,
                correction_version=0,
            )
        )
        collection = DispatchPriceCollection(artifact, records)
        with _connection(database_url, connect) as connection:
            interval_result = ingest_cycle(
                connection,
                collect=lambda **unused: collection,
                receipt_source_url=f"{claim.source_url}#{nested.member_name}",
            )
        price_count += len(records)
        applied_price_count += interval_result.price_count
        replayed_intervals += int(interval_result.replayed)

    with _connection(database_url, connect) as connection:
        completion: BackfillItemCompletion = ledger_factory(connection).complete(
            claim,
            records_imported=price_count,
        )
    return HistoricalPriceBackfillResult(
        len(extraction.nested),
        price_count,
        applied_price_count,
        replayed_intervals,
        artifact_result.replayed,
        completion.replayed,
    )


def run_price_backfill_claim(
    database_url: str,
    claim: BackfillClaim,
    start: datetime,
    end: datetime,
    *,
    connect: Callable[..., Any] = _connect,
    fetch: Callable[..., NemwebHttpResource] = fetch_nemweb_resource,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ledger_factory: Callable[[Any], Any] = PostgreSQLBackfillLedger,
    registrar_factory: Callable[[Any], Any] = PostgreSQLBackfillArtifactRegistrar,
    extract_archive: Callable[..., NemwebArchiveExtraction] = extract_nested_archive,
    extract_zip: Callable[[DispatchPriceArtifactRef, bytes], DispatchPriceArtifact] = extract_dispatch_price_zip,
    parse_csv: Callable[..., tuple[Any, ...]] = parse_dispatch_price_mms_csv,
    ingest_cycle: Callable[..., DispatchPriceIngestionResult] = run_price_collection_cycle,
) -> HistoricalPriceBackfillResult:
    _validated_range(database_url, claim, start, end)
    try:
        return _execute_price_claim(
            database_url,
            claim,
            start,
            end,
            connect=connect,
            fetch=fetch,
            clock=clock,
            ledger_factory=ledger_factory,
            registrar_factory=registrar_factory,
            extract_archive=extract_archive,
            extract_zip=extract_zip,
            parse_csv=parse_csv,
            ingest_cycle=ingest_cycle,
        )
    except Exception as error:
        try:
            with _connection(database_url, connect) as connection:
                ledger_factory(connection).fail(
                    claim,
                    error_summary=type(error).__name__,
                )
        except Exception:
            pass
        raise


__all__ = ["HistoricalPriceBackfillResult", "run_price_backfill_claim"]
