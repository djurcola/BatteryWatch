"""Standalone BatteryWatch HTTP API."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path

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
    Generator,
    GeneratorListResponse,
    HealthResponse,
    ProvenanceMetadata,
    SeriesPoint,
    SeriesResponse,
    SeriesSummary,
)

router = APIRouter()

MAX_WINDOW_SECONDS = 30 * 24 * 60 * 60


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
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