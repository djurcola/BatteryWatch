"""Runtime selection seams for fixture and database-backed requests."""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from .storage import PostgreSQLRepository, StorageRepository

ConnectionFactory = Callable[[str], Any]
RepositoryProvider = Callable[[], AbstractContextManager[StorageRepository]]


@contextmanager
def database_repository_provider(
    database_url: str | None,
    connection_factory: ConnectionFactory | None = None,
) -> Iterator[StorageRepository]:
    """Yield one PostgreSQL repository while owning one request connection."""

    if not database_url:
        raise RuntimeError("Database URL is not configured")
    factory = connection_factory or _default_connection_factory
    connection = factory(database_url)
    try:
        yield PostgreSQLRepository(connection)
    finally:
        connection.close()


def configured_database_url(explicit: str | None = None) -> str | None:
    """Return explicit database configuration or a supported environment value."""

    if explicit is not None:
        return explicit
    return os.getenv("BATTERYWATCH_DATABASE_URL") or os.getenv("DATABASE_URL")


class DatabaseHealthTracer:
    """Probe a configured database without importing its driver eagerly."""

    def __init__(
        self,
        database_url: str | None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._database_url = database_url
        self._connection_factory = connection_factory

    def check(self) -> bool:
        return self()

    def __call__(self) -> bool:
        if not self._database_url:
            return False

        try:
            connection_factory = self._connection_factory or _default_connection_factory
            connection = connection_factory(self._database_url)
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT 1")
                    return True
                finally:
                    cursor.close()
            finally:
                connection.close()
        except Exception:
            return False


def _default_connection_factory(database_url: str) -> Any:
    try:
        driver = importlib.import_module("psycopg")
        connect = driver.connect
    except Exception:
        return None
    return connect(database_url)


__all__ = [
    "ConnectionFactory",
    "RepositoryProvider",
    "DatabaseHealthTracer",
    "configured_database_url",
    "database_repository_provider",
]
