
"""Separate Dispatch SCADA collection and persistence runtime."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from importlib import import_module
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event
from typing import Any, Protocol

from .battery_assets import BatteryAsset, load_battery_assets
from .collector import DispatchScadaCollection, collect_latest_dispatch_scada
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
from .nemweb_dispatch_prices import DispatchPriceCollection, collect_latest_dispatch_prices
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
class CollectorCycleResult:
    scada: DispatchScadaIngestionResult
    prices: DispatchPriceIngestionResult

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


def run_collection_cycle(
    connection: Any,
    assets: Iterable[BatteryAsset],
    *,
    collect: Callable[..., DispatchScadaCollection] = collect_latest_dispatch_scada,
    ingestor_factory: Callable[[Any], _Ingestor] = PostgreSQLDispatchScadaIngestor,
    receipt_source_url: str | None = None,
) -> DispatchScadaIngestionResult:
    """Fetch, validate, map, and atomically persist one latest artifact."""

    asset_by_duid: dict[str, BatteryAsset] = {}
    for asset in assets:
        if asset.duid in asset_by_duid:
            raise ValueError("duplicate battery asset DUID")
        asset_by_duid[asset.duid] = asset
    if not asset_by_duid:
        raise ValueError("battery assets must not be empty")

    collection = collect(ingestion_version=0, correction_version=0)
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


def run_price_collection_cycle(
    connection: Any,
    *,
    collect: Callable[..., DispatchPriceCollection] = collect_latest_dispatch_prices,
    ingestor_factory: Callable[[Any], _PriceIngestor] = PostgreSQLDispatchPriceIngestor,
) -> DispatchPriceIngestionResult:
    """Fetch, validate, and atomically persist one official DispatchIS artifact."""

    collection = collect(ingestion_version=0, correction_version=0)
    artifact = collection.artifact
    reference = artifact.reference
    try:
        version = int(reference.source_artifact_id)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid DispatchIS artifact version") from None
    if not 0 <= version <= _MAX_BIGINT:
        raise ValueError("invalid DispatchIS artifact version")
    receipt = DispatchPriceArtifactReceipt(
        source_artifact_id=reference.source_artifact_id,
        source_url=reference.url,
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


def run_database_cycle(
    database_url: str,
    assets: Iterable[BatteryAsset],
    *,
    connect: Callable[..., Any] = _connect,
    collect: Callable[..., DispatchScadaCollection] = collect_latest_dispatch_scada,
    collect_prices: Callable[..., DispatchPriceCollection] = collect_latest_dispatch_prices,
    ingestor_factory: Callable[[Any], _Ingestor] = PostgreSQLDispatchScadaIngestor,
    price_ingestor_factory: Callable[[Any], _PriceIngestor] = PostgreSQLDispatchPriceIngestor,
) -> CollectorCycleResult:
    """Open one bounded database connection, run one cycle, and always close it."""

    if not isinstance(database_url, str) or not database_url:
        raise ValueError("database URL is required")
    connection = connect(database_url, connect_timeout=10)
    try:
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
    finally:
        connection.close()


def run_polling_loop(
    cycle: Callable[[], CollectorCycleResult],
    *,
    interval_seconds: int,
    wait: Callable[[float], bool],
) -> CollectorCycleResult:
    """Run immediately and stop cleanly when the interruptible wait is signalled."""

    if type(interval_seconds) is not int or not 30 <= interval_seconds <= 3600:
        raise ValueError("poll interval must be between 30 and 3600 seconds")
    while True:
        result = cycle()
        if wait(float(interval_seconds)):
            return result


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
            else int(environment.get("BATTERYWATCH_COLLECT_INTERVAL_SECONDS", "300"))
        )
        assets = load_battery_assets(assets_path)

        def cycle() -> CollectorCycleResult:
            return run_database_cycle(
                database_url,
                assets,
                connect=connect,
                collect=collect,
                collect_prices=collect_prices,
                ingestor_factory=ingestor_factory,
                price_ingestor_factory=price_ingestor_factory,
            )

        if arguments.once:
            result = cycle()
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
            )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "raw_observation_count": result.raw_observation_count,
                    "mapped_power_count": result.mapped_power_count,
                    "replayed": result.replayed,
                    "price_count": result.price_count,
                    "price_replayed": result.price_replayed,
                },
                sort_keys=True,
            )
        )
        return 0
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
    "CollectorCycleResult",
    "main",
    "run_collection_cycle",
    "run_database_cycle",
    "run_polling_loop",
    "run_price_collection_cycle",
]


if __name__ == "__main__":
    raise SystemExit(main())
