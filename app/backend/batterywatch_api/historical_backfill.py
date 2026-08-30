"""Deterministic orchestration over a supplied historical backfill plan."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Iterator

from .backfill_ledger import (
    BackfillClaim,
    BackfillEnsureResult,
    BackfillPlanItem,
    BackfillRunFinalization,
    BackfillRunSpec,
    PostgreSQLBackfillLedger,
)
from .battery_assets import BatteryAsset
from .historical_price_backfill import (
    HistoricalPriceBackfillResult,
    run_price_backfill_claim,
)
from .historical_nextday_backfill import (
    HistoricalNextDayBackfillResult,
    run_nextday_soc_backfill_claim,
)
from .historical_scada_backfill import (
    HistoricalScadaBackfillResult,
    run_scada_backfill_claim,
)


@dataclass(frozen=True, slots=True)
class HistoricalBackfillResult:
    ensure_result: BackfillEnsureResult
    claims: tuple[BackfillClaim, ...]
    scada_results: tuple[HistoricalScadaBackfillResult, ...]
    price_results: tuple[HistoricalPriceBackfillResult, ...]
    finalization: BackfillRunFinalization
    nextday_results: tuple[HistoricalNextDayBackfillResult, ...] = ()

    @property
    def claimed_count(self) -> int:
        return len(self.claims)

    @property
    def scada_raw_observation_count(self) -> int:
        return sum(result.raw_observation_count for result in self.scada_results)

    @property
    def scada_mapped_power_count(self) -> int:
        return sum(result.mapped_power_count for result in self.scada_results)

    @property
    def price_source_record_count(self) -> int:
        return sum(result.price_count for result in self.price_results)

    @property
    def price_applied_record_count(self) -> int:
        return sum(result.applied_price_count for result in self.price_results)

    @property
    def nextday_source_record_count(self) -> int:
        return sum(result.source_rows for result in self.nextday_results)

    @property
    def nextday_applied_record_count(self) -> int:
        return sum(result.effective_applied for result in self.nextday_results)

    @property
    def nextday_null_count(self) -> int:
        return sum(result.source_null_count for result in self.nextday_results)

    @property
    def nextday_percentage_count(self) -> int:
        return sum(result.percentage_count for result in self.nextday_results)

    @property
    def replayed_interval_count(self) -> int:
        return sum(result.replayed_interval_count for result in self.scada_results) + sum(
            result.replayed_interval_count for result in self.price_results
        ) + sum(result.raw_replayed for result in self.nextday_results)

    @property
    def replayed_outer_artifact_count(self) -> int:
        return sum(result.outer_artifact_replayed for result in self.scada_results) + sum(
            result.outer_artifact_replayed for result in self.price_results
        ) + sum(result.outer_replayed for result in self.nextday_results)


def _connect(database_url: str, *, connect_timeout: int) -> Any:
    return import_module("psycopg").connect(
        database_url,
        connect_timeout=connect_timeout,
    )


@contextmanager
def _connection(
    database_url: str,
    connect: Callable[..., Any],
) -> Iterator[Any]:
    connection = connect(database_url, connect_timeout=10)
    try:
        yield connection
    finally:
        connection.close()


def run_historical_backfill(
    database_url: str,
    assets: Iterable[BatteryAsset],
    spec: BackfillRunSpec,
    planned_items: Iterable[BackfillPlanItem],
    *,
    connect: Callable[..., Any] = _connect,
    ledger_factory: Callable[[Any], Any] = PostgreSQLBackfillLedger,
    run_scada: Callable[..., HistoricalScadaBackfillResult] = run_scada_backfill_claim,
    run_price: Callable[..., HistoricalPriceBackfillResult] = run_price_backfill_claim,
    run_nextday: Callable[..., HistoricalNextDayBackfillResult] = run_nextday_soc_backfill_claim,
) -> HistoricalBackfillResult:
    """Ensure, drain, and finalize one bounded supplied backfill plan."""

    if type(database_url) is not str or not database_url:
        raise ValueError("database URL is required")
    materialized_assets = tuple(assets)
    materialized_items = tuple(planned_items)

    with _connection(database_url, connect) as connection:
        ensure_result = ledger_factory(connection).ensure_run(
            spec,
            materialized_items,
        )

    claims: list[BackfillClaim] = []
    scada_results: list[HistoricalScadaBackfillResult] = []
    price_results: list[HistoricalPriceBackfillResult] = []
    nextday_results: list[HistoricalNextDayBackfillResult] = []
    while True:
        with _connection(database_url, connect) as connection:
            claim = ledger_factory(connection).claim_next(spec.run_id)
        if claim is None:
            break
        if len(claims) >= len(materialized_items):
            raise ValueError("historical backfill claim limit exceeded")
        claims.append(claim)
        if claim.feed == "dispatch_scada":
            scada_results.append(
                run_scada(
                    database_url,
                    materialized_assets,
                    claim,
                    spec.requested_start,
                    spec.requested_end,
                )
            )
        elif claim.feed == "dispatch_price":
            price_results.append(
                run_price(
                    database_url,
                    claim,
                    spec.requested_start,
                    spec.requested_end,
                )
            )
        elif claim.feed == "nextday_soc":
            nextday_results.append(
                run_nextday(
                    database_url,
                    materialized_assets,
                    claim,
                    spec.requested_start,
                    spec.requested_end,
                    spec.ingestion_version,
                )
            )
        else:
            raise ValueError("unsupported historical backfill feed")

    with _connection(database_url, connect) as connection:
        finalization = ledger_factory(connection).finalize(spec.run_id)

    return HistoricalBackfillResult(
        ensure_result,
        tuple(claims),
        tuple(scada_results),
        tuple(price_results),
        finalization,
        tuple(nextday_results),
    )


__all__ = ["HistoricalBackfillResult", "run_historical_backfill"]
