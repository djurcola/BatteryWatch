"""Typed response models for the BatteryWatch API."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Generator(ApiModel):
    duid: str
    site_name: str
    region: str
    capacity_mw: float = Field(gt=0)
    storage_capacity_mwh: float = Field(gt=0)
    data_start: datetime | None
    data_end: datetime | None
    data_status: Literal["fixture", "database"] = "fixture"

    @field_serializer("data_start", "data_end")
    def serialize_bounds(self, value: datetime | None) -> str | None:
        return None if value is None else utc_iso(value)


class GeneratorListResponse(ApiModel):
    generators: list[Generator]


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"
    service: str
    data_mode: Literal["fixture", "database"] = "fixture"


class SeriesPoint(ApiModel):
    timestamp: datetime
    power_mw: float
    soc_percent: float | None
    price_aud_per_mwh: float | None
    energy_mwh: float
    gross_value_aud: float | None
    charging_cost_aud: float | None
    net_energy_value_aud: float | None
    price_status: Literal["available", "negative", "missing"]

    @field_serializer("timestamp")
    def serialize_timestamp(self, value: datetime) -> str:
        return utc_iso(value)


class SeriesSummary(ApiModel):
    interval_count: int = Field(ge=0)
    interval_hours: float = Field(gt=0)
    total_energy_mwh: float
    exported_energy_mwh: float = Field(ge=0)
    imported_energy_mwh: float = Field(ge=0)
    gross_value_aud: float
    charging_cost_aud: float
    net_energy_value_aud: float


class Coverage(ApiModel):
    total_intervals: int = Field(ge=0)
    price_intervals: int = Field(ge=0)
    missing_price_intervals: int = Field(ge=0)
    price_coverage_percent: float = Field(ge=0, le=100)
    soc_intervals: int = Field(ge=0)
    missing_soc_intervals: int = Field(ge=0)
    soc_coverage_percent: float = Field(ge=0, le=100)


class EstimateMetadata(ApiModel):
    is_estimate: Literal[True] = True
    label: str
    disclaimer: str
    calculation: str


class ProvenanceMetadata(ApiModel):
    data_mode: Literal["deterministic_fixture", "database"] = "deterministic_fixture"
    power_source: str
    price_source: str
    soc_source: str
    timezone: Literal["UTC"] = "UTC"
    interval_minutes: Literal[5] = 5
    sign_convention: str
    calculation_version: str


class SeriesResponse(ApiModel):
    generator: Generator
    requested_start: datetime
    requested_end: datetime
    points: list[SeriesPoint]
    summary: SeriesSummary
    coverage: Coverage
    estimate: EstimateMetadata
    provenance: ProvenanceMetadata

    @field_serializer("requested_start", "requested_end")
    def serialize_bounds(self, value: datetime) -> str:
        return utc_iso(value)
