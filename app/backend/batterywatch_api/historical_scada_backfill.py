"""One bounded historical Dispatch SCADA backfill claim."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from importlib import import_module
from typing import Any, Iterator

from .backfill_artifacts import (
    BackfillArtifactReceipt,
    PostgreSQLBackfillArtifactRegistrar,
)
from .backfill_ledger import BackfillClaim, PostgreSQLBackfillLedger
from .battery_assets import BatteryAsset
from .collector import DispatchScadaCollection
from .collector_service import run_collection_cycle
from .dispatch_scada import parse_dispatch_scada_csv
from .nemweb_archives import (
    ARCHIVE_SOURCE,
    DISPATCH_SCADA_CURRENT_INDEX_URL,
    DISPATCH_SCADA_FEED,
    MAX_OUTER_ARCHIVE_BYTES,
    ArchivePlanItem,
    extract_nested_archive,
)
from .nemweb_dispatch_scada import (
    DispatchScadaArtifactRef,
    extract_dispatch_scada_zip,
)
from .nemweb_http import fetch_nemweb_resource


UTC = timezone.utc
_NEM_TIMEZONE = timezone(timedelta(hours=10))


@dataclass(frozen=True, slots=True)
class HistoricalScadaBackfillResult:
    interval_artifact_count: int
    raw_observation_count: int
    mapped_power_count: int
    replayed_interval_count: int
    outer_artifact_replayed: bool
    completion_replayed: bool


def _connect(database_url: str, *, connect_timeout: int) -> Any:
    return import_module("psycopg").connect(
        database_url, connect_timeout=connect_timeout
    )


@contextmanager
def _connection(
    database_url: str, connect: Callable[..., Any]
) -> Iterator[Any]:
    connection = connect(database_url, connect_timeout=10)
    try:
        yield connection
    finally:
        connection.close()


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(f"invalid {name}")
    try:
        if value.utcoffset() is None:
            raise ValueError(f"invalid {name}")
        return value.astimezone(UTC)
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise ValueError(f"invalid {name}") from None


def _last_modified(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid Last-Modified header") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid Last-Modified header")
    return parsed.astimezone(UTC)


def _validate_inputs(
    database_url: str,
    claim: BackfillClaim,
    start: datetime,
    end: datetime,
) -> tuple[datetime, datetime]:
    if type(database_url) is not str or not database_url:
        raise ValueError("database URL is required")
    if type(claim) is not BackfillClaim or claim.feed != DISPATCH_SCADA_FEED:
        raise ValueError("invalid Dispatch SCADA backfill claim")
    range_start = _utc(start, "backfill start")
    range_end = _utc(end, "backfill end")
    if range_end <= range_start:
        raise ValueError("invalid backfill range")
    return range_start, range_end


def _run_scada_backfill_claim(
    database_url: str,
    assets: Iterable[BatteryAsset],
    claim: BackfillClaim,
    start: datetime,
    end: datetime,
    *,
    connect: Callable[..., Any] = _connect,
    fetch: Callable[..., Any] = fetch_nemweb_resource,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ledger_factory: Callable[[Any], Any] = PostgreSQLBackfillLedger,
    registrar_factory: Callable[[Any], Any] = PostgreSQLBackfillArtifactRegistrar,
    extract_archive: Callable[..., Any] = extract_nested_archive,
    extract_zip: Callable[..., Any] = extract_dispatch_scada_zip,
    parse_csv: Callable[..., Any] = parse_dispatch_scada_csv,
    ingest_cycle: Callable[..., Any] = run_collection_cycle,
) -> HistoricalScadaBackfillResult:
    range_start, range_end = _validate_inputs(database_url, claim, start, end)
    materialized_assets = tuple(assets)

    resource = fetch(claim.source_url, max_bytes=MAX_OUTER_ARCHIVE_BYTES)
    downloaded_at = _utc(clock(), "download time")
    source_last_modified = _last_modified(resource.last_modified)
    plan_item = ArchivePlanItem(
        claim.feed, claim.report_date, ARCHIVE_SOURCE, claim.source_url
    )

    with _connection(database_url, connect) as connection:
        artifact_result = registrar_factory(connection).record(
            BackfillArtifactReceipt(
                claim,
                downloaded_at,
                source_last_modified,
                resource.body,
            )
        )

    extraction = extract_archive(
        plan_item,
        resource.body,
        outer_url=claim.source_url,
        start=range_start,
        end=range_end,
    )
    if not extraction.nested:
        raise ValueError("archive contains no selected Dispatch SCADA intervals")

    raw_count = 0
    mapped_count = 0
    replayed_count = 0
    for nested in extraction.nested:
        reference = DispatchScadaArtifactRef(
            DISPATCH_SCADA_CURRENT_INDEX_URL + nested.member_name,
            nested.member_name,
            nested.source_artifact_id,
            nested.interval_timestamp,
        )
        artifact = extract_zip(reference, nested.raw_bytes)
        records = parse_csv(
            artifact.csv_payload,
            source_artifact_id=nested.source_artifact_id,
            ingestion_version=0,
            correction_version=0,
            naive_timezone=_NEM_TIMEZONE,
        )
        collection = DispatchScadaCollection(artifact, tuple(records))
        with _connection(database_url, connect) as connection:
            ingestion = ingest_cycle(
                connection,
                materialized_assets,
                collect=lambda **unused: collection,
                receipt_source_url=f"{extraction.outer.url}#{nested.member_name}",
            )
        raw_count += ingestion.raw_observation_count
        mapped_count += ingestion.mapped_power_count
        replayed_count += int(ingestion.replayed)

    with _connection(database_url, connect) as connection:
        completion = ledger_factory(connection).complete(
            claim, records_imported=raw_count
        )

    return HistoricalScadaBackfillResult(
        len(extraction.nested),
        raw_count,
        mapped_count,
        replayed_count,
        artifact_result.replayed,
        completion.replayed,
    )


def run_scada_backfill_claim(
    database_url: str,
    assets: Iterable[BatteryAsset],
    claim: BackfillClaim,
    start: datetime,
    end: datetime,
    *,
    connect: Callable[..., Any] = _connect,
    fetch: Callable[..., Any] = fetch_nemweb_resource,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ledger_factory: Callable[[Any], Any] = PostgreSQLBackfillLedger,
    registrar_factory: Callable[[Any], Any] = PostgreSQLBackfillArtifactRegistrar,
    extract_archive: Callable[..., Any] = extract_nested_archive,
    extract_zip: Callable[..., Any] = extract_dispatch_scada_zip,
    parse_csv: Callable[..., Any] = parse_dispatch_scada_csv,
    ingest_cycle: Callable[..., Any] = run_collection_cycle,
) -> HistoricalScadaBackfillResult:
    """Fetch, validate, and persist exactly one claimed daily SCADA item."""

    _validate_inputs(database_url, claim, start, end)
    try:
        return _run_scada_backfill_claim(
            database_url,
            assets,
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
                    claim, error_summary=type(error).__name__
                )
        except Exception:
            pass
        raise


__all__ = ["HistoricalScadaBackfillResult", "run_scada_backfill_claim"]
