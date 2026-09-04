"""Dispatch SCADA and DispatchIS collection runtime."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from importlib import import_module
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event
from typing import Any, Protocol

from .battery_assets import BatteryAsset, load_battery_assets
from .collector import (
    DispatchScadaCollection,
    collect_dispatch_scada_artifact,
    collect_latest_dispatch_scada,
    discover_current_dispatch_scada,
)
from .collector_ledger import (
    CURRENT_ARTIFACTS_PER_FEED_LIMIT,
    CurrentArtifactClaim,
    CurrentArtifactReference,
    PostgreSQLCurrentArtifactLedger,
    bounded_error_summary,
)
from .dispatch_price_ingestion import (
    DispatchPriceArtifactReceipt,
    DispatchPriceIngestionResult,
    PostgreSQLDispatchPriceIngestor,
)
from .dispatch_scada_ingestion import (
    DispatchScadaArtifactReceipt,
    DispatchScadaIngestionResult,
    PostgreSQLDispatchScadaIngestor,
    RawDispatchScadaObservation,
)
from .nemweb_dispatch_prices import (
    DispatchPriceArtifactRef,
    DispatchPriceCollection,
    collect_dispatch_price_artifact,
    collect_latest_dispatch_prices,
    discover_current_dispatch_prices,
)
from .nemweb_dispatch_scada import DispatchScadaArtifactRef
from .nemweb_http import NemwebHttpResource, fetch_nemweb_resource
from .storage import GeneratorMetadata, GeneratorPower5m


_MAX_BIGINT = (1 << 63) - 1


class _Ingestor(Protocol):
    def ingest(
        self,
        receipt: DispatchScadaArtifactReceipt,
        observations: Iterable[RawDispatchScadaObservation],
        generators: Iterable[GeneratorMetadata] = (),
        power_records: Iterable[GeneratorPower5m] = (),
    ) -> DispatchScadaIngestionResult: ...


class _PriceIngestor(Protocol):
    def ingest(
        self,
        receipt: DispatchPriceArtifactReceipt,
        records: Iterable[Any],
    ) -> DispatchPriceIngestionResult: ...


@dataclass(frozen=True, slots=True)
class CollectorFeedCounters:
    """Bounded work counters for one CURRENT feed in one poll cycle."""

    discovered: int = 0
    attempted: int = 0
    completed: int = 0
    replayed: int = 0
    missed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "attempted": self.attempted,
            "completed": self.completed,
            "replayed": self.replayed,
            "missed": self.missed,
        }


@dataclass(frozen=True, slots=True)
class CollectorCycleCounters:
    """Aggregate bounded work counters for one poll cycle."""

    discovered: int = 0
    attempted: int = 0
    completed: int = 0
    replayed: int = 0
    missed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "attempted": self.attempted,
            "completed": self.completed,
            "replayed": self.replayed,
            "missed": self.missed,
        }


@dataclass(frozen=True, slots=True)
class CollectorCycleResult:
    scada: DispatchScadaIngestionResult
    prices: DispatchPriceIngestionResult
    counters: CollectorCycleCounters = field(default_factory=CollectorCycleCounters)
    errors: tuple[str, ...] = ()
    dispatch_scada: CollectorFeedCounters = field(default_factory=CollectorFeedCounters)
    dispatch_price: CollectorFeedCounters = field(default_factory=CollectorFeedCounters)

    @property
    def status(self) -> str:
        return "error" if self.errors else "ok"

    @property
    def discovered(self) -> int:
        return self.counters.discovered

    @property
    def attempted(self) -> int:
        return self.counters.attempted

    @property
    def completed(self) -> int:
        return self.counters.completed

    @property
    def missed(self) -> int:
        return self.counters.missed

    @property
    def replayed_count(self) -> int:
        return self.counters.replayed

    @property
    def scada_counters(self) -> CollectorFeedCounters:
        return self.dispatch_scada

    @property
    def price_counters(self) -> CollectorFeedCounters:
        return self.dispatch_price

    @property
    def dispatch_scada_counters(self) -> CollectorFeedCounters:
        return self.dispatch_scada

    @property
    def dispatch_price_counters(self) -> CollectorFeedCounters:
        return self.dispatch_price

    @property
    def raw_observation_count(self) -> int:
        return self.scada.raw_observation_count

    @property
    def mapped_power_count(self) -> int:
        return self.scada.mapped_power_count

    @property
    def replayed(self) -> bool:
        return self.scada.replayed

    @property
    def price_count(self) -> int:
        return self.prices.price_count

    @property
    def price_replayed(self) -> bool:
        return self.prices.replayed


def _connect(database_url: str, *, connect_timeout: int) -> Any:
    return import_module("psycopg").connect(
        database_url, connect_timeout=connect_timeout
    )


def _artifact_version(collection: DispatchScadaCollection) -> int:
    source_artifact_id = collection.artifact.reference.source_artifact_id
    try:
        version = int(source_artifact_id)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid Dispatch SCADA artifact version") from None
    if not 0 <= version <= _MAX_BIGINT:
        raise ValueError("invalid Dispatch SCADA artifact version")
    return version


def _asset_map(assets: Iterable[BatteryAsset]) -> dict[str, BatteryAsset]:
    asset_by_duid: dict[str, BatteryAsset] = {}
    for asset in assets:
        if asset.duid in asset_by_duid:
            raise ValueError("duplicate battery asset DUID")
        asset_by_duid[asset.duid] = asset
    if not asset_by_duid:
        raise ValueError("battery assets must not be empty")
    return asset_by_duid


def _ingest_scada_collection(
    connection: Any,
    asset_by_duid: dict[str, BatteryAsset],
    collection: DispatchScadaCollection,
    ingestor_factory: Callable[[Any], _Ingestor],
    *,
    receipt_source_url: str | None = None,
) -> DispatchScadaIngestionResult:
    """Map configured DUIDs while retaining every parsed raw observation."""

    version = _artifact_version(collection)
    artifact = collection.artifact
    reference = artifact.reference
    receipt = DispatchScadaArtifactReceipt(
        source_artifact_id=reference.source_artifact_id,
        source_url=(reference.url if receipt_source_url is None else receipt_source_url),
        zip_filename=reference.zip_filename,
        csv_member_name=artifact.csv_member_name,
        report_timestamp=reference.report_timestamp,
        zip_sha256=artifact.zip_sha256,
        raw_zip=artifact.raw_zip,
    )

    raw_rows: list[RawDispatchScadaObservation] = []
    mapped_power: list[GeneratorPower5m] = []
    mapped_duids: set[str] = set()
    for record in collection.records:
        if record.source_id != reference.source_artifact_id:
            raise ValueError("record source artifact does not match collection")
        canonical_record = GeneratorPower5m(
            generator_id=record.generator_id,
            interval_start=record.interval_start,
            power_mw=record.power_mw,
            source_id=record.source_id,
            source_timestamp=record.source_timestamp,
            ingestion_version=version,
            correction_version=record.correction_version,
        )
        raw_rows.append(
            RawDispatchScadaObservation(
                source_artifact_id=reference.source_artifact_id,
                duid=canonical_record.generator_id,
                interval_start=canonical_record.interval_start,
                power_mw=canonical_record.power_mw,
                source_timestamp=canonical_record.source_timestamp,
                ingestion_version=canonical_record.ingestion_version,
                correction_version=canonical_record.correction_version,
            )
        )
        if canonical_record.generator_id in asset_by_duid:
            mapped_power.append(canonical_record)
            mapped_duids.add(canonical_record.generator_id)

    generators = tuple(
        GeneratorMetadata(
            generator_id=asset.duid,
            site_name=asset.site_name,
            region=asset.region,
            capacity_mw=asset.capacity_mw,
            storage_capacity_mwh=asset.storage_capacity_mwh,
            source_id=asset.source_id,
            source_timestamp=asset.source_timestamp,
            ingestion_version=1,
        )
        for duid, asset in sorted(asset_by_duid.items())
        if duid in mapped_duids
    )
    return ingestor_factory(connection).ingest(
        receipt,
        tuple(raw_rows),
        generators=generators,
        power_records=tuple(mapped_power),
    )


def _ingest_price_collection(
    connection: Any,
    collection: DispatchPriceCollection,
    ingestor_factory: Callable[[Any], _PriceIngestor],
    *,
    receipt_source_url: str | None = None,
) -> DispatchPriceIngestionResult:
    """Preserve price status/provenance while assigning the artifact version."""

    reference = collection.artifact.reference
    try:
        version = int(reference.source_artifact_id)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid DispatchIS artifact version") from None
    if not 0 <= version <= _MAX_BIGINT:
        raise ValueError("invalid DispatchIS artifact version")
    artifact = collection.artifact
    receipt = DispatchPriceArtifactReceipt(
        source_artifact_id=reference.source_artifact_id,
        source_url=(reference.url if receipt_source_url is None else receipt_source_url),
        zip_filename=reference.zip_filename,
        csv_member_name=artifact.csv_member_name,
        report_timestamp=reference.report_timestamp,
        zip_sha256=artifact.zip_sha256,
        raw_zip=artifact.raw_zip,
    )
    records = tuple(
        replace(record, ingestion_version=version)
        for record in collection.records
    )
    return ingestor_factory(connection).ingest(receipt, records)


def run_collection_cycle(
    connection: Any,
    assets: Iterable[BatteryAsset],
    *,
    collect: Callable[..., DispatchScadaCollection] = collect_latest_dispatch_scada,
    ingestor_factory: Callable[[Any], _Ingestor] = PostgreSQLDispatchScadaIngestor,
    receipt_source_url: str | None = None,
) -> DispatchScadaIngestionResult:
    """Compatibility entry point for callers explicitly requesting one file."""

    asset_by_duid = _asset_map(assets)
    collection = collect(ingestion_version=0, correction_version=0)
    return _ingest_scada_collection(
        connection,
        asset_by_duid,
        collection,
        ingestor_factory,
        receipt_source_url=receipt_source_url,
    )


def run_price_collection_cycle(
    connection: Any,
    *,
    collect: Callable[..., DispatchPriceCollection] = collect_latest_dispatch_prices,
    ingestor_factory: Callable[[Any], _PriceIngestor] = PostgreSQLDispatchPriceIngestor,
    receipt_source_url: str | None = None,
) -> DispatchPriceIngestionResult:
    """Compatibility entry point for callers explicitly requesting one file."""

    collection = collect(ingestion_version=0, correction_version=0)
    return _ingest_price_collection(
        connection,
        collection,
        ingestor_factory,
        receipt_source_url=receipt_source_url,
    )


def _source_reference(claim: CurrentArtifactClaim) -> Any:
    reference = claim.reference
    if reference.feed == "dispatch_scada":
        return DispatchScadaArtifactRef(
            url=reference.source_url,
            zip_filename=reference.zip_filename,
            source_artifact_id=reference.source_artifact_id,
            report_timestamp=reference.report_timestamp,
        )
    return DispatchPriceArtifactRef(
        url=reference.source_url,
        zip_filename=reference.zip_filename,
        source_artifact_id=reference.source_artifact_id,
        report_timestamp=reference.report_timestamp,
    )


def _artifact_matches_claim(claim: CurrentArtifactClaim, artifact: Any) -> bool:
    reference = artifact.reference
    return (
        reference.url == claim.source_url
        and reference.zip_filename == claim.zip_filename
        and reference.source_artifact_id == claim.source_artifact_id
        and reference.report_timestamp == claim.report_timestamp
    )


def _cycle_error(feed: str, operation: str, error: Exception) -> str:
    """Return only a bounded, feed-qualified exception type."""

    return f"{feed}:{operation}:{type(error).__name__}"


def _error_summary(error: Exception) -> str:
    """Never persist exception text, which can contain source or secret data."""

    return bounded_error_summary(type(error).__name__)


def _add_scada_results(
    current: DispatchScadaIngestionResult,
    added: DispatchScadaIngestionResult,
) -> DispatchScadaIngestionResult:
    return DispatchScadaIngestionResult(
        current.raw_observation_count + added.raw_observation_count,
        current.mapped_power_count + added.mapped_power_count,
        current.replayed or added.replayed,
    )


def _add_price_results(
    current: DispatchPriceIngestionResult,
    added: DispatchPriceIngestionResult,
) -> DispatchPriceIngestionResult:
    return DispatchPriceIngestionResult(
        current.price_count + added.price_count,
        current.replayed or added.replayed,
    )


def run_current_collection_cycle(
    connection: Any,
    assets: Iterable[BatteryAsset],
    *,
    fetch: Callable[..., NemwebHttpResource] = fetch_nemweb_resource,
    discover_scada: Callable[..., Iterable[DispatchScadaArtifactRef]] = discover_current_dispatch_scada,
    discover_prices: Callable[..., Iterable[DispatchPriceArtifactRef]] = discover_current_dispatch_prices,
    collect_scada_artifact: Callable[..., DispatchScadaCollection] = collect_dispatch_scada_artifact,
    collect_price_artifact: Callable[..., DispatchPriceCollection] = collect_dispatch_price_artifact,
    ledger_factory: Callable[[Any], Any] = PostgreSQLCurrentArtifactLedger,
    ingestor_factory: Callable[[Any], _Ingestor] = PostgreSQLDispatchScadaIngestor,
    price_ingestor_factory: Callable[[Any], _PriceIngestor] = PostgreSQLDispatchPriceIngestor,
    max_artifacts_per_feed: int = CURRENT_ARTIFACTS_PER_FEED_LIMIT,
) -> CollectorCycleResult:
    """Reconcile every listing entry and process one bounded claim batch/feed."""

    asset_by_duid = _asset_map(assets)
    if (
        type(max_artifacts_per_feed) is not int
        or not 0 < max_artifacts_per_feed <= CURRENT_ARTIFACTS_PER_FEED_LIMIT
    ):
        raise ValueError("invalid CURRENT artifact limit")

    zero_scada = DispatchScadaIngestionResult(0, 0, False)
    zero_prices = DispatchPriceIngestionResult(0, False)
    cycle_errors: list[str] = []
    try:
        ledger = ledger_factory(connection)
    except Exception as error:
        error_type = type(error).__name__
        cycle_errors.extend(
            (
                f"dispatch_scada:ledger:{error_type}",
                f"dispatch_price:ledger:{error_type}",
            )
        )
        empty_counts = CollectorFeedCounters()
        return CollectorCycleResult(
            zero_scada,
            zero_prices,
            errors=tuple(cycle_errors),
            dispatch_scada=empty_counts,
            dispatch_price=empty_counts,
        )

    def process_feed(
        feed: str,
        discover: Callable[..., Iterable[Any]],
        collect_one: Callable[..., Any],
    ) -> tuple[Any, CollectorFeedCounters]:
        scada_result: DispatchScadaIngestionResult = zero_scada
        price_result: DispatchPriceIngestionResult = zero_prices
        discovered_count = attempted_count = completed_count = 0
        replayed_count = missed_count = 0

        try:
            source_references = tuple(discover(fetch=fetch))
            current_references = tuple(
                CurrentArtifactReference(
                    feed=feed,
                    source_artifact_id=reference.source_artifact_id,
                    source_url=reference.url,
                    zip_filename=reference.zip_filename,
                    report_timestamp=reference.report_timestamp,
                )
                for reference in source_references
            )
            discovered_count = len(current_references)
        except Exception as error:
            cycle_errors.append(_cycle_error(feed, "discover", error))
            return (
                scada_result if feed == "dispatch_scada" else price_result,
                CollectorFeedCounters(discovered_count, 0, 0, 0, 0),
            )

        try:
            ledger.reconcile(current_references)
        except Exception as error:
            cycle_errors.append(_cycle_error(feed, "reconcile", error))
            return (
                scada_result if feed == "dispatch_scada" else price_result,
                CollectorFeedCounters(discovered_count, 0, 0, 0, 0),
            )

        try:
            claims = tuple(
                ledger.claim_pending(feed, limit=max_artifacts_per_feed)
            )
        except Exception as error:
            cycle_errors.append(_cycle_error(feed, "claim", error))
            return (
                scada_result if feed == "dispatch_scada" else price_result,
                CollectorFeedCounters(discovered_count, 0, 0, 0, 0),
            )

        def mark_missed(claim: CurrentArtifactClaim, error: Exception) -> None:
            nonlocal missed_count
            try:
                ledger.miss(claim, error_summary=_error_summary(error))
            except Exception as miss_error:
                cycle_errors.append(_cycle_error(feed, "miss", miss_error))
                return
            missed_count += 1

        for claim in claims:
            attempted_count += 1
            try:
                source_reference = _source_reference(claim)
                collected = collect_one(
                    source_reference,
                    ingestion_version=0,
                    correction_version=0,
                    fetch=fetch,
                )
                if not _artifact_matches_claim(claim, collected.artifact):
                    raise ValueError("collected artifact does not match CURRENT claim")
            except Exception as error:
                cycle_errors.append(_cycle_error(feed, "collect", error))
                mark_missed(claim, error)
                continue

            try:
                if feed == "dispatch_scada":
                    added_result = _ingest_scada_collection(
                        connection,
                        asset_by_duid,
                        collected,
                        ingestor_factory,
                    )
                    scada_result = _add_scada_results(scada_result, added_result)
                else:
                    added_result = _ingest_price_collection(
                        connection,
                        collected,
                        price_ingestor_factory,
                    )
                    price_result = _add_price_results(price_result, added_result)
            except Exception as error:
                cycle_errors.append(_cycle_error(feed, "ingest", error))
                mark_missed(claim, error)
                continue

            try:
                ledger.complete(claim)
            except Exception as error:
                # The ingestor commits before completion.  Retrying this claim
                # is safe and must not mark committed data as a false miss.
                cycle_errors.append(_cycle_error(feed, "complete", error))
                continue

            completed_count += 1
            if added_result.replayed:
                replayed_count += 1

        result = scada_result if feed == "dispatch_scada" else price_result
        return (
            result,
            CollectorFeedCounters(
                discovered_count,
                attempted_count,
                completed_count,
                replayed_count,
                missed_count,
            ),
        )

    scada, scada_counts = process_feed(
        "dispatch_scada", discover_scada, collect_scada_artifact
    )
    prices, price_counts = process_feed(
        "dispatch_price", discover_prices, collect_price_artifact
    )
    aggregate = CollectorCycleCounters(
        scada_counts.discovered + price_counts.discovered,
        scada_counts.attempted + price_counts.attempted,
        scada_counts.completed + price_counts.completed,
        scada_counts.replayed + price_counts.replayed,
        scada_counts.missed + price_counts.missed,
    )
    return CollectorCycleResult(
        scada,
        prices,
        aggregate,
        tuple(cycle_errors),
        scada_counts,
        price_counts,
    )


def run_database_cycle(
    database_url: str,
    assets: Iterable[BatteryAsset],
    *,
    connect: Callable[..., Any] = _connect,
    collect: Callable[..., DispatchScadaCollection] = collect_latest_dispatch_scada,
    collect_prices: Callable[..., DispatchPriceCollection] = collect_latest_dispatch_prices,
    ingestor_factory: Callable[[Any], _Ingestor] = PostgreSQLDispatchScadaIngestor,
    price_ingestor_factory: Callable[[Any], _PriceIngestor] = PostgreSQLDispatchPriceIngestor,
    fetch: Callable[..., NemwebHttpResource] = fetch_nemweb_resource,
    discover_scada: Callable[..., Iterable[DispatchScadaArtifactRef]] = discover_current_dispatch_scada,
    discover_prices: Callable[..., Iterable[DispatchPriceArtifactRef]] = discover_current_dispatch_prices,
    collect_scada_artifact: Callable[..., DispatchScadaCollection] = collect_dispatch_scada_artifact,
    collect_price_artifact: Callable[..., DispatchPriceCollection] = collect_dispatch_price_artifact,
    ledger_factory: Callable[[Any], Any] | None = None,
    max_artifacts_per_feed: int = CURRENT_ARTIFACTS_PER_FEED_LIMIT,
) -> CollectorCycleResult:
    """Open one connection and run the ledger-backed CURRENT cycle by default."""

    if not isinstance(database_url, str) or not database_url:
        raise ValueError("database URL is required")
    connection = connect(database_url, connect_timeout=10)
    try:
        # Preserve direct callers that explicitly injected the old one-file
        # collectors.  The service defaults never take this branch.
        legacy_requested = (
            collect is not collect_latest_dispatch_scada
            or collect_prices is not collect_latest_dispatch_prices
        )
        if ledger_factory is None and legacy_requested:
            scada = run_collection_cycle(
                connection,
                assets,
                collect=collect,
                ingestor_factory=ingestor_factory,
            )
            prices = run_price_collection_cycle(
                connection,
                collect=collect_prices,
                ingestor_factory=price_ingestor_factory,
            )
            return CollectorCycleResult(scada, prices)
        return run_current_collection_cycle(
            connection,
            assets,
            fetch=fetch,
            discover_scada=discover_scada,
            discover_prices=discover_prices,
            collect_scada_artifact=collect_scada_artifact,
            collect_price_artifact=collect_price_artifact,
            ledger_factory=(
                PostgreSQLCurrentArtifactLedger
                if ledger_factory is None
                else ledger_factory
            ),
            ingestor_factory=ingestor_factory,
            price_ingestor_factory=price_ingestor_factory,
            max_artifacts_per_feed=max_artifacts_per_feed,
        )
    finally:
        connection.close()


def run_polling_loop(
    cycle: Callable[[], CollectorCycleResult],
    *,
    interval_seconds: int,
    wait: Callable[[float], bool],
    emit: Callable[[CollectorCycleResult], None] | None = None,
) -> CollectorCycleResult:
    """Run immediately, emit each result, and stop on signal or cycle error."""

    if type(interval_seconds) is not int or not 30 <= interval_seconds <= 3600:
        raise ValueError("poll interval must be between 30 and 3600 seconds")
    while True:
        result = cycle()
        if emit is not None:
            emit(result)
        if result.status != "ok":
            return result
        if wait(float(interval_seconds)):
            return result


def _result_payload(result: CollectorCycleResult) -> dict[str, Any]:
    aggregate = result.counters.as_dict()
    payload: dict[str, Any] = {
        "status": result.status,
        "raw_observation_count": result.raw_observation_count,
        "mapped_power_count": result.mapped_power_count,
        "price_count": result.price_count,
        "price_replayed": result.price_replayed,
        **aggregate,
        "dispatch_scada": result.dispatch_scada.as_dict(),
        "dispatch_price": result.dispatch_price.as_dict(),
        "counters": {
            "aggregate": aggregate,
            "dispatch_scada": result.dispatch_scada.as_dict(),
            "dispatch_price": result.dispatch_price.as_dict(),
        },
    }
    if result.errors:
        payload["error_type"] = result.errors[0]
        payload["error_types"] = list(result.errors)
    return payload


def _cycle_failure(error: Exception) -> CollectorCycleResult:
    error_type = type(error).__name__
    return CollectorCycleResult(
        DispatchScadaIngestionResult(0, 0, False),
        DispatchPriceIngestionResult(0, False),
        errors=(
            f"dispatch_scada:cycle:{error_type}",
            f"dispatch_price:cycle:{error_type}",
        ),
    )


def main(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    connect: Callable[..., Any] = _connect,
    collect: Callable[..., DispatchScadaCollection] = collect_latest_dispatch_scada,
    collect_prices: Callable[..., DispatchPriceCollection] = collect_latest_dispatch_prices,
    ingestor_factory: Callable[[Any], _Ingestor] = PostgreSQLDispatchScadaIngestor,
    price_ingestor_factory: Callable[[Any], _PriceIngestor] = PostgreSQLDispatchPriceIngestor,
) -> int:
    """Run one cycle or the supervised long-lived collector process."""

    parser = argparse.ArgumentParser(prog="batterywatch-collector")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--assets-path", type=Path)
    parser.add_argument("--interval-seconds", type=int)
    arguments = parser.parse_args(argv)
    environment = os.environ if environ is None else environ
    try:
        database_url = environment.get("BATTERYWATCH_DATABASE_URL", "")
        assets_path = arguments.assets_path or Path(
            environment.get(
                "BATTERYWATCH_ASSETS_PATH",
                str(Path(__file__).resolve().parents[2] / "config/battery_assets.json"),
            )
        )
        interval_seconds = (
            arguments.interval_seconds
            if arguments.interval_seconds is not None
            else int(environment.get("BATTERYWATCH_COLLECT_INTERVAL_SECONDS", "180"))
        )
        assets = load_battery_assets(assets_path)

        def cycle() -> CollectorCycleResult:
            try:
                return run_database_cycle(
                    database_url,
                    assets,
                    connect=connect,
                    collect=collect,
                    collect_prices=collect_prices,
                    ingestor_factory=ingestor_factory,
                    price_ingestor_factory=price_ingestor_factory,
                )
            except Exception as error:
                return _cycle_failure(error)

        def emit_result(result: CollectorCycleResult) -> None:
            print(json.dumps(_result_payload(result), sort_keys=True), flush=True)

        if arguments.once:
            result = cycle()
            emit_result(result)
        else:
            stop = Event()

            def request_stop(signum: int, frame: Any) -> None:
                del signum, frame
                stop.set()

            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
            result = run_polling_loop(
                cycle,
                interval_seconds=interval_seconds,
                wait=stop.wait,
                emit=emit_result,
            )
        return 0 if result.status == "ok" else 1
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(
            json.dumps(
                {"status": "error", "error_type": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


__all__ = [
    "CollectorCycleCounters",
    "CollectorCycleResult",
    "CollectorFeedCounters",
    "main",
    "run_collection_cycle",
    "run_current_collection_cycle",
    "run_database_cycle",
    "run_polling_loop",
    "run_price_collection_cycle",
]


if __name__ == "__main__":
    raise SystemExit(main())
