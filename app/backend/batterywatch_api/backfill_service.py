"""Bounded operator command for historical NEMWeb power and price backfills."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from .backfill_ledger import BackfillPlanItem, BackfillRunSpec
from .battery_assets import BatteryAsset, load_battery_assets
from .historical_backfill import HistoricalBackfillResult, run_historical_backfill
from .nemweb_archives import (
    DISPATCHIS_PRICE_FEED,
    DISPATCH_SCADA_FEED,
    plan_archive_range,
)
from .nextday_archives import (
    NextDayMonthlyArchiveRef,
    plan_nextday_monthly_archives,
)

UTC = timezone.utc
_NEM_TIMEZONE = timezone(timedelta(hours=10))
_FEED_MAP = {
    "power": DISPATCH_SCADA_FEED,
    "price": DISPATCHIS_PRICE_FEED,
}
_LEDGER_FEED_MAP = {
    DISPATCH_SCADA_FEED: "dispatch_scada",
    DISPATCHIS_PRICE_FEED: "dispatch_price",
}


@dataclass(frozen=True, slots=True)
class OperatorBackfillPlan:
    spec: BackfillRunSpec
    items: tuple[BackfillPlanItem, ...]
    missing_nextday_months: tuple[date, ...]


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("UTC timestamp is required")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("invalid UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return parsed.astimezone(UTC)


def _candidate_dates(start: datetime, end: datetime) -> tuple[date, ...]:
    first = start.astimezone(_NEM_TIMEZONE).date() - timedelta(days=1)
    last = end.astimezone(_NEM_TIMEZONE).date()
    return tuple(first + timedelta(days=index) for index in range((last - first).days + 1))


def build_operator_plan_details(
    run_id: str,
    start: datetime,
    end: datetime,
    *,
    feeds: Iterable[str] = ("power", "price"),
    ingestion_version: int = 1,
    nextday_archives: Iterable[NextDayMonthlyArchiveRef] = (),
) -> OperatorBackfillPlan:
    """Build a bounded plan and retain explicit unavailable SOC months."""

    try:
        requested = tuple(feeds)
    except (TypeError, ValueError):
        raise ValueError("invalid backfill feeds") from None
    allowed = {*_FEED_MAP, "soc"}
    if (
        not requested
        or len(set(requested)) != len(requested)
        or any(type(feed) is not str or feed not in allowed for feed in requested)
    ):
        raise ValueError("invalid backfill feeds")

    archive_feeds = tuple(
        archive_feed
        for name, archive_feed in _FEED_MAP.items()
        if name in requested
    )
    daily_items: tuple[BackfillPlanItem, ...] = ()
    normalized_start: datetime | None = None
    normalized_end: datetime | None = None
    if archive_feeds:
        candidates = _candidate_dates(start, end)
        archived_dates = {feed: candidates for feed in archive_feeds}
        archive_plan = plan_archive_range(
            start,
            end,
            feeds=archive_feeds,
            archived_dates=archived_dates,
        )
        normalized_start = archive_plan.start
        normalized_end = archive_plan.end
        daily_items = tuple(
            BackfillPlanItem(
                _LEDGER_FEED_MAP[item.feed],
                item.report_date,
                item.url,
            )
            for item in archive_plan.items
        )

    soc_items: tuple[BackfillPlanItem, ...] = ()
    missing_months: tuple[date, ...] = ()
    if "soc" in requested:
        soc_plan = plan_nextday_monthly_archives(start, end, nextday_archives)
        if normalized_start is None:
            normalized_start = soc_plan.start
            normalized_end = soc_plan.end
        soc_items = tuple(
            BackfillPlanItem("nextday_soc", item.report_month, item.url)
            for item in soc_plan.items
        )
        missing_months = soc_plan.missing_months

    if normalized_start is None or normalized_end is None:
        raise ValueError("invalid backfill feeds")
    return OperatorBackfillPlan(
        BackfillRunSpec(run_id, normalized_start, normalized_end, ingestion_version),
        daily_items + soc_items,
        missing_months,
    )


def build_operator_plan(
    run_id: str,
    start: datetime,
    end: datetime,
    *,
    feeds: Iterable[str] = ("power", "price"),
    ingestion_version: int = 1,
    nextday_archives: Iterable[NextDayMonthlyArchiveRef] = (),
) -> tuple[BackfillRunSpec, tuple[BackfillPlanItem, ...]]:
    """Build canonical bounded ledger items from explicit operator inputs."""

    details = build_operator_plan_details(
        run_id,
        start,
        end,
        feeds=feeds,
        ingestion_version=ingestion_version,
        nextday_archives=nextday_archives,
    )
    return details.spec, details.items


def _summary(
    result: HistoricalBackfillResult,
    planned_item_count: int,
    spec: BackfillRunSpec,
) -> dict[str, Any]:
    progress = result.finalization.progress
    return {
        "status": progress.status,
        "run_id": progress.run_id,
        "requested_start": spec.requested_start.isoformat().replace("+00:00", "Z"),
        "requested_end": spec.requested_end.isoformat().replace("+00:00", "Z"),
        "planned_item_count": planned_item_count,
        "claimed_item_count": result.claimed_count,
        "created": result.ensure_result.created,
        "resumed": result.ensure_result.resumed,
        "recovered_item_count": result.ensure_result.recovered_count,
        "scada_raw_observation_count": result.scada_raw_observation_count,
        "scada_mapped_power_count": result.scada_mapped_power_count,
        "price_source_record_count": result.price_source_record_count,
        "price_applied_record_count": result.price_applied_record_count,
        "replayed_interval_count": result.replayed_interval_count,
        "replayed_outer_artifact_count": result.replayed_outer_artifact_count,
        "completion_replayed": result.finalization.replayed,
        "total_items": progress.total,
        "pending_items": progress.pending,
        "running_items": progress.running,
        "completed_items": progress.completed,
        "failed_items": progress.failed,
        "total_attempts": progress.total_attempts,
    }


def main(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    load_assets: Callable[[Path], Iterable[BatteryAsset]] = load_battery_assets,
    run: Callable[..., HistoricalBackfillResult] = run_historical_backfill,
) -> int:
    parser = argparse.ArgumentParser(prog="batterywatch-backfill")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--feeds", default="power,price")
    parser.add_argument("--ingestion-version", type=int, default=1)
    parser.add_argument("--assets-path", type=Path)
    arguments = parser.parse_args(argv)
    environment = os.environ if environ is None else environ

    try:
        database_url = environment.get("BATTERYWATCH_DATABASE_URL", "")
        if not database_url:
            raise ValueError("database URL is required")
        start = _parse_utc(arguments.start)
        end = _parse_utc(arguments.end)
        spec, items = build_operator_plan(
            arguments.run_id,
            start,
            end,
            feeds=tuple(arguments.feeds.split(",")),
            ingestion_version=arguments.ingestion_version,
        )
        assets_path = arguments.assets_path or Path(
            environment.get(
                "BATTERYWATCH_ASSETS_PATH",
                str(Path(__file__).resolve().parents[2] / "config/battery_assets.json"),
            )
        )
        result = run(
            database_url,
            tuple(load_assets(assets_path)),
            spec,
            items,
        )
        print(json.dumps(_summary(result, len(items), spec), sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "error_type": type(error).__name__,
                    "run_id": arguments.run_id,
                    "status": "error",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


__all__ = [
    "OperatorBackfillPlan",
    "build_operator_plan",
    "build_operator_plan_details",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
