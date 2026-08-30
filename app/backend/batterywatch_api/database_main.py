"""Database-only ASGI entrypoint for the activated BatteryWatch service."""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any

from fastapi import FastAPI

from .main import create_app


def create_database_application(
    environ: Mapping[str, str] | None = None,
    *,
    connection_factory: Any = None,
    health_tracer: Any = None,
    repository_provider: Any = None,
) -> FastAPI:
    """Create an application whose database mode cannot be changed by env."""

    environment = os.environ if environ is None else environ
    options: dict[str, Any] = {}
    if connection_factory is not None:
        options["connection_factory"] = connection_factory
    if health_tracer is not None:
        options["health_tracer"] = health_tracer
    if repository_provider is not None:
        options["repository_provider"] = repository_provider
    return create_app(
        data_mode="database",
        database_url=environment.get("BATTERYWATCH_DATABASE_URL"),
        **options,
    )


app = create_database_application()

__all__ = ["app", "create_database_application"]
