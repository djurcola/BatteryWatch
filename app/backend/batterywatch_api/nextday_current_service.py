"""One-shot operator command for bounded Next Day CURRENT collection."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from datetime import date
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from .battery_assets import BatteryAsset, load_battery_assets
from .nextday_current_collection import (
    NextDayCurrentCollectionResult,
    NextDayCurrentProjectionTotals,
    run_nextday_current_cycle,
)


_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$", re.ASCII)
_SAFE_FAILURE_LOCATIONS = frozenset(
    {
        "fetch",
        "downloaded_at",
        "registration",
        "parse",
        "soc_projection",
        "fcas_projection",
        "cycle",
    }
)
_DEFAULT_ASSETS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "battery_assets.json"
)


def _strict_report_date(value: str) -> date:
    """Parse only the extended ISO calendar-date spelling YYYY-MM-DD."""

    if type(value) is not str or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value, re.ASCII
    ):
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from None
    if type(parsed) is not date:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD")
    return parsed


def _safe_error_type(value: object) -> str:
    if type(value) is str and _SAFE_NAME.fullmatch(value):
        return value
    return "Error"


def _safe_location(value: object) -> str:
    if type(value) is str and value in _SAFE_FAILURE_LOCATIONS:
        return value
    return "unknown"


def _date_text(value: object) -> str | None:
    if type(value) is not date:
        return None
    return value.isoformat()


def _projection_payload(
    totals: NextDayCurrentProjectionTotals,
) -> dict[str, int]:
    return {
        "attempted": totals.attempted,
        "completed": totals.completed,
        "replayed": totals.replayed,
        "failed": totals.failed,
        "remaining": totals.remaining,
        "source_rows": totals.source_rows,
        "raw_inserted": totals.raw_inserted,
        "raw_replayed": totals.raw_replayed,
        "effective_candidates": totals.effective_candidates,
        "effective_applied": totals.effective_applied,
        "effective_replayed": totals.effective_replayed,
        "source_null_count": totals.source_null_count,
        "percentage_count": totals.percentage_count,
        "reported_service_count": totals.reported_service_count,
    }


def _result_payload(result: NextDayCurrentCollectionResult) -> dict[str, Any]:
    """Serialize only bounded, non-sensitive cycle evidence."""

    failures = [
        {
            "report_date": _date_text(failure.report_date),
            "error_type": _safe_error_type(failure.error_type),
            "location": _safe_location(failure.location),
        }
        for failure in result.failures[:2]
    ]
    completed_dates = [
        text
        for item in result.completed_report_dates[:2]
        if (text := _date_text(item)) is not None
    ]
    return {
        "status": (
            "ok" if result.failed == 0 and not result.failures else "error"
        ),
        "attempted": result.attempted,
        "completed": result.completed,
        "replayed": result.replayed,
        "failed": result.failed,
        "remaining": result.remaining,
        "completed_report_dates": completed_dates,
        "min_completed_report_date": _date_text(
            result.min_completed_report_date
        ),
        "max_completed_report_date": _date_text(
            result.max_completed_report_date
        ),
        "failures": failures,
        "soc": _projection_payload(result.soc),
        "fcas": _projection_payload(result.fcas),
    }


def _print_runtime_error(error: BaseException) -> None:
    print(
        json.dumps(
            {
                "status": "error",
                "error_type": _safe_error_type(type(error).__name__),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def main(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    load_assets: Callable[[Path], Iterable[BatteryAsset]] = load_battery_assets,
    run: Callable[..., NextDayCurrentCollectionResult] = run_nextday_current_cycle,
) -> int:
    """Run exactly one bounded CURRENT cycle and emit one JSON result."""

    parser = argparse.ArgumentParser(prog="batterywatch-nextday")
    parser.add_argument("--assets-path", type=Path)
    parser.add_argument("--limit", type=int, choices=(1, 2), default=2)
    parser.add_argument("--start-date", type=_strict_report_date)
    parser.add_argument("--end-date", type=_strict_report_date)
    arguments = parser.parse_args(argv)

    if (arguments.start_date is None) != (arguments.end_date is None):
        parser.error("--start-date and --end-date must be supplied together")
    if (
        arguments.start_date is not None
        and arguments.end_date is not None
        and arguments.start_date > arguments.end_date
    ):
        parser.error("--start-date must not follow --end-date")

    environment = os.environ if environ is None else environ
    try:
        database_url = environment.get("BATTERYWATCH_DATABASE_URL", "")
        if not database_url:
            raise ValueError("database URL is required")
        assets_path = arguments.assets_path or Path(
            environment.get("BATTERYWATCH_ASSETS_PATH", str(_DEFAULT_ASSETS_PATH))
        )
        assets = tuple(load_assets(assets_path))
        result = run(
            database_url,
            assets,
            limit=arguments.limit,
            start_date=arguments.start_date,
            end_date=arguments.end_date,
        )
        if type(result) is not NextDayCurrentCollectionResult:
            raise ValueError("invalid CURRENT cycle result")
        print(json.dumps(_result_payload(result), sort_keys=True))
        return 0 if result.failed == 0 and not result.failures else 1
    except Exception as error:
        _print_runtime_error(error)
        return 1


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
