"""Standalone BatteryWatch HTTP API."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import isfinite
import os
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .fixtures import FIXTURE_DUID, INTERVAL, fixture_points, generator_metadata
from .runtime import (
    ConnectionFactory,
    DatabaseHealthTracer,
    RepositoryProvider,
    configured_database_url,
    database_repository_provider,
)
from .models import (
    Coverage,
    EstimateMetadata,
    FcasCoverage,
    FcasLatestFinalizedMetadata,
    FcasPoint,
    FcasPublicationState,
    FcasResponse,
    FcasServiceName,
    FcasServicePoint,
    FcasServiceSummary,
    Generator,
    GeneratorListResponse,
    HealthResponse,
    ProvenanceMetadata,
    SeriesPoint,
    SeriesResponse,
    SeriesSummary,
)
from .storage import FCAS_CLEARANCE_EPSILON_MW, FCAS_SERVICES

router = APIRouter()

MAX_WINDOW_SECONDS = 30 * 24 * 60 * 60
NEM_TIMEZONE = timezone(timedelta(hours=10))


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=400, detail=f"{field} must include a UTC offset")
    return value.astimezone(timezone.utc)


def _point(raw) -> SeriesPoint:
    power = raw.power_mw
    price = raw.price_aud_per_mwh
    status = "missing" if price is None else (
        "negative" if price < 0 else "available"
    )
    if power is None:
        energy = gross = charging = net = None
    else:
        energy = power * 5 / 60
        if price is None:
            gross = charging = net = None
        elif power > 0:
            gross = energy * price
            charging = 0.0
            net = gross
        elif power < 0:
            gross = 0.0
            charging = abs(energy) * price
            net = -charging
        else:
            gross = charging = net = 0.0
    return SeriesPoint(
        timestamp=raw.timestamp,
        power_mw=power,
        soc_percent=raw.soc_percent,
        price_aud_per_mwh=price,
        energy_mwh=energy,
        gross_value_aud=gross,
        charging_cost_aud=charging,
        net_energy_value_aud=net,
        price_status=status,
    )


def _database_generator(record) -> Generator:
    def optional_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("database generator bounds must include a UTC offset")
        return value.astimezone(timezone.utc)

    return Generator(
        duid=record.generator_id,
        site_name=record.site_name,
        region=record.region,
        capacity_mw=record.capacity_mw,
        storage_capacity_mwh=record.storage_capacity_mwh,
        data_start=optional_utc(record.data_start),
        data_end=optional_utc(record.data_end),
        data_status="database",
    )


def _tracer_is_healthy(tracer: object) -> bool:
    try:
        if callable(tracer):
            return bool(tracer())
        check = getattr(tracer, "check", None)
        return callable(check) and bool(check())
    except Exception:
        return False


@router.get("/api/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    if request.app.state.data_mode == "database":
        if not _tracer_is_healthy(request.app.state.health_tracer):
            raise HTTPException(status_code=503, detail="Database health unavailable")
        return HealthResponse(service="batterywatch-api", data_mode="database")
    return HealthResponse(service="batterywatch-api")


def _ensure_fixture_runtime(request: Request) -> None:
    if request.app.state.data_mode != "fixture":
        raise HTTPException(status_code=503, detail="Database data reads unavailable")


def _database_bounds(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    if start is None or end is None:
        raise HTTPException(
            status_code=400,
            detail="Database series requires explicit start and end",
        )
    try:
        parsed_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="start and end must be ISO-8601 timestamps",
        ) from None
    requested_start = _utc(parsed_start, "start")
    requested_end = _utc(parsed_end, "end")
    if requested_end <= requested_start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if (requested_end - requested_start).total_seconds() > MAX_WINDOW_SECONDS:
        raise HTTPException(
            status_code=400,
            detail="Requested range exceeds the 30-day limit",
        )
    return requested_start, requested_end


def _fcas_bounds(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    if start is None or end is None:
        raise HTTPException(
            status_code=400,
            detail="FCAS requires explicit start and end",
        )
    try:
        parsed_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="start and end must be ISO-8601 timestamps",
        ) from None
    requested_start = _utc(parsed_start, "start")
    requested_end = _utc(parsed_end, "end")
    if requested_end <= requested_start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if (requested_end - requested_start).total_seconds() > MAX_WINDOW_SECONDS:
        raise HTTPException(
            status_code=400,
            detail="Requested range exceeds the 30-day limit",
        )
    return requested_start, requested_end


def _fcas_services(value: str | None) -> tuple[FcasServiceName, ...]:
    if value is None:
        return FCAS_SERVICES
    parts = value.split(",")
    if not parts or any(part not in FCAS_SERVICES for part in parts):
        raise HTTPException(status_code=400, detail="Invalid FCAS service filter")
    if len(parts) != len(set(parts)):
        raise HTTPException(status_code=400, detail="Invalid FCAS service filter")
    requested = set(parts)
    return tuple(
        service
        for service in FCAS_SERVICES
        if service in requested
    )


def _fcas_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"invalid FCAS {field}")
    try:
        normalized = float(cast(Any, value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid FCAS {field}") from exc
    if not isfinite(normalized) or normalized < 0:
        raise ValueError(f"invalid FCAS {field}")
    return normalized


def _fcas_status(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or type(value) is not int or value not in {0, 1, 2, 3, 4}:
        raise ValueError("invalid FCAS enablement status")
    return value


def _fcas_raw_field(raw: object, field: str) -> object:
    if isinstance(raw, Mapping):
        return raw[field]
    return getattr(raw, field)


def _fcas_service_point(raw: object | None) -> FcasServicePoint:
    if raw is None:
        target = status = actual = None
    elif isinstance(raw, Mapping):
        if set(raw) != {
            "target_mw",
            "enablement_status",
            "actual_availability_mw",
        }:
            raise ValueError("invalid FCAS service value")
        target = _fcas_number(raw["target_mw"], "target_mw")
        status = _fcas_status(raw["enablement_status"])
        actual = _fcas_number(
            raw["actual_availability_mw"],
            "actual_availability_mw",
        )
    else:
        target = _fcas_number(_fcas_raw_field(raw, "target_mw"), "target_mw")
        status = _fcas_status(_fcas_raw_field(raw, "enablement_status"))
        actual = _fcas_number(
            _fcas_raw_field(raw, "actual_availability_mw"),
            "actual_availability_mw",
        )
    enabled = status in (1, 3)
    trapped = status == 3
    stranded = status == 4
    cleared = target is not None and target > FCAS_CLEARANCE_EPSILON_MW
    return FcasServicePoint(
        target_mw=target,
        enablement_status=status,
        actual_availability_mw=actual,
        enabled=enabled,
        trapped=trapped,
        stranded=stranded,
        cleared=cleared,
        participating=cleared and enabled,
        response_verified=False,
    )


def _fcas_rows_in_window(
    rows: object,
    generator: str,
    start: datetime,
    end: datetime,
) -> list[object]:
    if isinstance(rows, (str, bytes)):
        raise ValueError("invalid FCAS rows")
    try:
        materialized = list(rows)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("invalid FCAS rows") from exc
    selected = []
    for row in materialized:
        if getattr(row, "generator_id") != generator:
            raise ValueError("FCAS row generator does not match request")
        interval_start = getattr(row, "interval_start")
        if start <= interval_start < end:
            selected.append(row)
    timestamps = [getattr(row, "interval_start") for row in selected]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate FCAS interval rows")
    return sorted(selected, key=lambda row: getattr(row, "interval_start"))


def _fcas_projected_points(
    rows: list[object],
    selected_services: tuple[FcasServiceName, ...],
    requested_start: datetime,
    requested_end: datetime,
) -> tuple[
    list[FcasPoint],
    dict[datetime, dict[FcasServiceName, FcasServicePoint]],
]:
    projected: dict[datetime, dict[FcasServiceName, FcasServicePoint]] = {}
    for row in rows:
        service_values = getattr(row, "services")
        if not isinstance(service_values, Mapping) or set(service_values) != set(FCAS_SERVICES):
            raise ValueError("invalid FCAS service map")
        all_values: dict[str, FcasServicePoint] = {}
        for service in FCAS_SERVICES:
            raw_value = _fcas_raw_field(service_values, service)
            if raw_value is None:
                raise ValueError("invalid FCAS service value")
            all_values[service] = _fcas_service_point(raw_value)
        projected[getattr(row, "interval_start")] = {
            service: all_values[service]
            for service in selected_services
        }

    points: list[FcasPoint] = []
    timestamp = _first_aligned_timestamp(requested_start)
    while timestamp < requested_end:
        values = projected.get(timestamp, {})
        points.append(
            FcasPoint(
                timestamp=timestamp,
                services={
                    service: values.get(service) or _fcas_service_point(None)
                    for service in selected_services
                },
            )
        )
        timestamp += INTERVAL
    return points, projected


def _fcas_service_summaries(
    projected: Mapping[datetime, Mapping[FcasServiceName, FcasServicePoint]],
    selected_services: tuple[FcasServiceName, ...],
) -> dict[FcasServiceName, FcasServiceSummary]:
    summaries: dict[FcasServiceName, FcasServiceSummary] = {}
    for service in selected_services:
        values = [point[service] for point in projected.values()]
        targets = [point.target_mw for point in values if point.target_mw is not None]
        actual_availability = [
            point.actual_availability_mw
            for point in values
            if point.actual_availability_mw is not None
        ]
        summaries[service] = FcasServiceSummary(
            reported_intervals=sum(
                point.target_mw is not None
                or point.enablement_status is not None
                or point.actual_availability_mw is not None
                for point in values
            ),
            enabled_intervals=sum(point.enabled for point in values),
            cleared_intervals=sum(point.cleared for point in values),
            participating_intervals=sum(point.participating for point in values),
            trapped_intervals=sum(point.trapped for point in values),
            stranded_intervals=sum(point.stranded for point in values),
            max_target_mw=max(targets) if targets else None,
            max_actual_availability_mw=(
                max(actual_availability) if actual_availability else None
            ),
        )
    return summaries


def _fcas_latest_finalized(rows: list[object]) -> FcasLatestFinalizedMetadata | None:
    if not rows:
        return None
    latest = max(rows, key=lambda row: getattr(row, "interval_start"))
    return FcasLatestFinalizedMetadata(
        interval_start=getattr(latest, "interval_start"),
        report_timestamp=getattr(latest, "report_timestamp"),
        downloaded_at=getattr(latest, "downloaded_at"),
        source_artifact_sha256=getattr(latest, "source_artifact_sha256"),
        dispatch_interval=getattr(latest, "dispatch_interval"),
        intervention=getattr(latest, "intervention"),
        run_number=getattr(latest, "run_number"),
    )


def _fcas_publication_state(
    rows: list[object],
    expected_intervals: int,
    requested_start: datetime,
    requested_end: datetime,
    now: datetime,
) -> FcasPublicationState:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("FCAS publication clock must be timezone-aware")
    current_day_start = datetime.combine(
        now.astimezone(NEM_TIMEZONE).date(),
        datetime.min.time(),
        tzinfo=NEM_TIMEZONE,
    ).astimezone(timezone.utc)
    if rows:
        if len(rows) == expected_intervals:
            return "available"
        expected_timestamps = set()
        timestamp = _first_aligned_timestamp(requested_start)
        while timestamp < requested_end:
            expected_timestamps.add(timestamp)
            timestamp += INTERVAL
        observed_timestamps = {
            getattr(row, "interval_start")
            for row in rows
        }
        missing_timestamps = expected_timestamps - observed_timestamps
        if missing_timestamps and all(
            timestamp >= current_day_start
            for timestamp in missing_timestamps
        ):
            return "not_yet_public"
        return "partial"
    current_day_end = current_day_start + timedelta(days=1)
    if requested_start < current_day_end and requested_end > current_day_start:
        return "not_yet_public"
    return "no_data"


def _database_fcas(
    request: Request,
    generator: str,
    start: str | None,
    end: str | None,
    services: str | None,
) -> FcasResponse:
    requested_start, requested_end = _fcas_bounds(start, end)
    selected_services = _fcas_services(services)
    found = False
    result: FcasResponse | None = None
    try:
        with request.app.state.repository_provider() as repository:
            metadata = repository.read_generator(generator)
            if metadata is not None:
                found = True
                rows = _fcas_rows_in_window(
                    repository.list_fcas(
                        generator,
                        start=requested_start,
                        end=requested_end,
                    ),
                    generator,
                    requested_start,
                    requested_end,
                )
                points, projected = _fcas_projected_points(
                    rows,
                    selected_services,
                    requested_start,
                    requested_end,
                )
                expected_intervals = len(points)
                observed_intervals = len(rows)
                result = FcasResponse(
                    generator=_database_generator(metadata),
                    requested_start=requested_start,
                    requested_end=requested_end,
                    selected_services=list(selected_services),
                    points=points,
                    coverage=FcasCoverage(
                        expected_intervals=expected_intervals,
                        observed_intervals=observed_intervals,
                        missing_intervals=expected_intervals - observed_intervals,
                        coverage_percent=(
                            observed_intervals / expected_intervals * 100
                            if expected_intervals
                            else 0
                        ),
                    ),
                    latest_finalized=_fcas_latest_finalized(rows),
                    publication_state=_fcas_publication_state(
                        rows,
                        expected_intervals,
                        requested_start,
                        requested_end,
                        request.app.state.fcas_clock(),
                    ),
                    service_summaries=_fcas_service_summaries(
                        projected,
                        selected_services,
                    ),
                )
    except Exception:
        raise HTTPException(status_code=503, detail="Database FCAS unavailable") from None
    if not found:
        raise HTTPException(status_code=404, detail="Unknown generator")
    assert result is not None
    return result


def _series_response(
    generator: Generator,
    requested_start: datetime,
    requested_end: datetime,
    raw_points,
    provenance: ProvenanceMetadata,
) -> SeriesResponse:
    points = [
        _point(item)
        for item in raw_points
        if requested_start <= item.timestamp < requested_end
    ]
    observed_power = [item for item in points if item.power_mw is not None]
    observed_price = [item for item in points if item.price_aud_per_mwh is not None]
    soc_points = [item for item in points if item.soc_percent is not None]
    exported = sum(
        item.energy_mwh for item in observed_power if item.energy_mwh is not None and item.energy_mwh > 0
    )
    imported = sum(
        -item.energy_mwh for item in observed_power if item.energy_mwh is not None and item.energy_mwh < 0
    )
    gross = sum(item.gross_value_aud or 0 for item in points)
    charging = sum(item.charging_cost_aud or 0 for item in points)
    expected_intervals = len(points)
    observed_power_intervals = len(observed_power)
    observed_price_intervals = len(observed_price)
    missing_power_intervals = expected_intervals - observed_power_intervals
    missing_price_intervals = expected_intervals - observed_price_intervals
    both_missing_intervals = sum(
        1
        for item in points
        if item.power_mw is None and item.price_aud_per_mwh is None
    )
    return SeriesResponse(
        generator=generator,
        requested_start=requested_start,
        requested_end=requested_end,
        points=points,
        summary=SeriesSummary(
            interval_count=len(points),
            interval_hours=5 / 60,
            total_energy_mwh=sum(
                item.energy_mwh for item in points if item.energy_mwh is not None
            ),
            exported_energy_mwh=exported,
            imported_energy_mwh=imported,
            gross_value_aud=gross,
            charging_cost_aud=charging,
            net_energy_value_aud=gross - charging,
        ),
        coverage=Coverage(
            expected_intervals=expected_intervals,
            observed_power_intervals=observed_power_intervals,
            observed_price_intervals=observed_price_intervals,
            missing_power_intervals=missing_power_intervals,
            missing_price_intervals=missing_price_intervals,
            both_missing_intervals=both_missing_intervals,
            power_coverage_percent=(
                observed_power_intervals / expected_intervals * 100
                if expected_intervals
                else 0
            ),
            total_intervals=expected_intervals,
            price_intervals=observed_price_intervals,
            price_coverage_percent=(
                observed_price_intervals / expected_intervals * 100
                if expected_intervals
                else 0
            ),
            soc_intervals=len(soc_points),
            missing_soc_intervals=expected_intervals - len(soc_points),
            soc_coverage_percent=(
                len(soc_points) / expected_intervals * 100
                if expected_intervals
                else 0
            ),
        ),
        estimate=EstimateMetadata(
            label="Estimated gross energy value",
            disclaimer="Estimate only; excludes efficiency losses, auxiliary use, degradation, FCAS, network charges, contracts, tax, and fees.",
            calculation="energy_mwh = power_mw × 5/60; value_aud = energy_mwh × price_aud_per_mwh",
        ),
        provenance=provenance,
    )


@dataclass(frozen=True, slots=True)
class _DatabasePoint:
    timestamp: datetime
    power_mw: float | None
    soc_percent: float | None
    price_aud_per_mwh: float | None


def _first_aligned_timestamp(value: datetime) -> datetime:
    floored = value.replace(minute=(value.minute // 5) * 5, second=0, microsecond=0)
    return floored if floored == value else floored + INTERVAL


def _database_points(
    power_rows,
    soc_rows,
    price_rows,
    requested_start: datetime,
    requested_end: datetime,
) -> list[_DatabasePoint]:
    power_by_timestamp = {
        row.timestamp: row.power_mw
        for row in power_rows
        if requested_start <= row.timestamp < requested_end
    }
    soc_by_timestamp = {
        row.timestamp: row.soc_percent
        for row in soc_rows
        if requested_start <= row.timestamp < requested_end
    }
    price_by_timestamp = {
        row.timestamp: row.price_aud_per_mwh
        for row in price_rows
        if requested_start <= row.timestamp < requested_end
    }
    timestamp = _first_aligned_timestamp(requested_start)
    points: list[_DatabasePoint] = []
    while timestamp < requested_end:
        points.append(
            _DatabasePoint(
                timestamp=timestamp,
                power_mw=power_by_timestamp.get(timestamp),
                soc_percent=soc_by_timestamp.get(timestamp),
                price_aud_per_mwh=price_by_timestamp.get(timestamp),
            )
        )
        timestamp += INTERVAL
    return points


def _safe_source_label(rows, fallback: str) -> str:
    source_ids = {
        row.source_id
        for row in rows
        if isinstance(getattr(row, "source_id", None), str)
        and 0 < len(row.source_id) <= 128
        and row.source_id.isascii()
        and all(character.isalnum() or character in "._-" for character in row.source_id)
    }
    return ", ".join(sorted(source_ids)) or fallback


def _database_series(
    request: Request,
    generator: str,
    start: str | None,
    end: str | None,
) -> SeriesResponse:
    if (
        (start is None or end is None)
        and not request.app.state.database_url
        and not request.app.state.repository_provider_injected
    ):
        raise HTTPException(status_code=503, detail="Database series unavailable")
    requested_start, requested_end = _database_bounds(start, end)
    found = False
    result: SeriesResponse | None = None
    try:
        with request.app.state.repository_provider() as repository:
            metadata = repository.read_generator(generator)
            if metadata is not None:
                found = True
                power_rows = repository.list_power(
                    generator, start=requested_start, end=requested_end
                )
                soc_rows = repository.list_soc(
                    generator, start=requested_start, end=requested_end
                )
                price_rows = repository.list_prices(
                    metadata.region, start=requested_start, end=requested_end
                )
                result = _series_response(
                    _database_generator(metadata),
                    requested_start,
                    requested_end,
                    _database_points(
                        power_rows,
                        soc_rows,
                        price_rows,
                        requested_start,
                        requested_end,
                    ),
                    ProvenanceMetadata(
                        data_mode="database",
                        power_source=_safe_source_label(
                            power_rows, "database generator_power_5m"
                        ),
                        price_source=_safe_source_label(
                            price_rows, "database nem_price_5m"
                        ),
                        soc_source=_safe_source_label(
                            soc_rows, "database generator_soc_5m"
                        ),
                        sign_convention="positive discharge/export; negative charge/import",
                        calculation_version="estimate-v1",
                    ),
                )
    except Exception:
        raise HTTPException(status_code=503, detail="Database series unavailable") from None
    if not found:
        raise HTTPException(status_code=404, detail="Unknown generator")
    assert result is not None
    return result


@router.get("/api/generators", response_model=GeneratorListResponse)
def generators(request: Request) -> GeneratorListResponse:
    if request.app.state.data_mode == "fixture":
        return GeneratorListResponse(generators=[Generator(**generator_metadata())])
    try:
        with request.app.state.repository_provider() as repository:
            records = repository.list_generators()
        return GeneratorListResponse(
            generators=[_database_generator(record) for record in records]
        )
    except Exception:
        raise HTTPException(
            status_code=503, detail="Database generators unavailable"
        ) from None


@router.get("/api/fcas", response_model=FcasResponse)
def fcas(
    request: Request,
    generator: str = Query(..., min_length=1),
    start: str | None = None,
    end: str | None = None,
    services: str | None = None,
) -> FcasResponse:
    if request.app.state.data_mode != "database":
        raise HTTPException(status_code=503, detail="Database FCAS unavailable")
    return _database_fcas(request, generator, start, end, services)


@router.get("/api/series", response_model=SeriesResponse)
def series(
    request: Request,
    generator: str = Query(..., min_length=1),
    start: str | None = None,
    end: str | None = None,
) -> SeriesResponse:
    if request.app.state.data_mode == "database":
        return _database_series(request, generator, start, end)
    _ensure_fixture_runtime(request)
    if generator != FIXTURE_DUID:
        raise HTTPException(status_code=404, detail="Unknown generator")
    raw_points = fixture_points()
    default_start = raw_points[0].timestamp
    default_end = raw_points[-1].timestamp + INTERVAL
    try:
        requested_start = _utc(datetime.fromisoformat(start.replace("Z", "+00:00")), "start") if start else default_start
        requested_end = _utc(datetime.fromisoformat(end.replace("Z", "+00:00")), "end") if end else default_end
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="start and end must be ISO-8601 timestamps") from exc
    if requested_end <= requested_start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if (requested_end - requested_start).total_seconds() > MAX_WINDOW_SECONDS:
        raise HTTPException(status_code=400, detail="Requested range exceeds the 30-day limit")

    return _series_response(
        Generator(**generator_metadata()),
        requested_start,
        requested_end,
        raw_points,
        ProvenanceMetadata(
            power_source="deterministic five-minute fixture",
            price_source="AEMO RRP-shaped deterministic fixture",
            soc_source="deterministic fixture; nullable when unavailable",
            sign_convention="positive discharge/export; negative charge/import",
            calculation_version="estimate-v1",
        ),
    )


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(
    mode: str = "fixture",
    health_tracer: object | None = None,
    database_url: str | None = None,
    connection_factory: ConnectionFactory | None = None,
    data_mode: str | None = None,
    repository_provider: RepositoryProvider | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    selected_mode = mode if data_mode is None else data_mode
    if selected_mode not in {"fixture", "database"}:
        raise ValueError("mode must be fixture or database")
    application = FastAPI(title="BatteryWatch API", version="0.1.0")
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    configured_url = configured_database_url(database_url) if selected_mode == "database" else None
    application.state.data_mode = selected_mode
    application.state.database_url = configured_url
    application.state.connection_factory = connection_factory
    application.state.repository_provider_injected = repository_provider is not None
    application.state.fcas_clock = clock or (lambda: datetime.now(timezone.utc))
    application.state.repository_provider = (
        repository_provider
        if repository_provider is not None
        else (
            (lambda: database_repository_provider(configured_url, connection_factory))
            if selected_mode == "database"
            else None
        )
    )
    application.state.health_tracer = (
        health_tracer
        if health_tracer is not None
        else DatabaseHealthTracer(configured_url, connection_factory)
        if selected_mode == "database"
        else None
    )
    application.include_router(router)
    if frontend_dist.is_dir():
        application.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return application


def create_runtime_app(
    environ: Mapping[str, str] | None = None,
    *,
    health_tracer: object | None = None,
    connection_factory: ConnectionFactory | None = None,
    repository_provider: RepositoryProvider | None = None,
) -> FastAPI:
    """Create the deployed application from explicit fail-closed environment mode."""

    environment = os.environ if environ is None else environ
    database_url = environment.get("BATTERYWATCH_DATABASE_URL") or environment.get(
        "DATABASE_URL"
    )
    return create_app(
        data_mode=environment.get("BATTERYWATCH_DATA_MODE", "fixture"),
        database_url=database_url,
        health_tracer=health_tracer,
        connection_factory=connection_factory,
        repository_provider=repository_provider,
    )


app = create_runtime_app()