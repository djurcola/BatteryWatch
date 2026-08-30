"""Focused database generator-list tracer tests."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import inspect
import unittest

from fastapi.testclient import TestClient

import batterywatch_api.main as main
from batterywatch_api.storage import (
    GeneratorMetadata,
    InMemoryRepository,
    StorageRepository,
)


class DatabaseGeneratorApiTests(unittest.TestCase):
    def test_factory_accepts_a_repository_provider(self):
        self.assertIn("repository_provider", inspect.signature(main.create_app).parameters)

    def test_storage_repository_declares_list_generators(self):
        self.assertTrue(callable(getattr(StorageRepository, "list_generators", None)))

    def test_database_generators_maps_repository_metadata_as_database_utc_values(self):
        record = GeneratorMetadata(
            generator_id="DB-1",
            site_name="Database Battery",
            region="QLD1",
            capacity_mw=3.5,
            storage_capacity_mwh=7.0,
            source_id="registry",
            source_timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=timezone(timedelta(hours=10))),
            ingestion_version=1,
            data_start=datetime(2026, 1, 1, 10, 5, tzinfo=timezone(timedelta(hours=10))),
            data_end=datetime(2026, 1, 1, 11, 5, tzinfo=timezone(timedelta(hours=10))),
        )

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def list_generators(self):
                    return (record,)

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get("/api/generators")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "generators": [
                    {
                        "duid": "DB-1",
                        "site_name": "Database Battery",
                        "region": "QLD1",
                        "capacity_mw": 3.5,
                        "storage_capacity_mwh": 7.0,
                        "data_start": "2026-01-01T00:05:00Z",
                        "data_end": "2026-01-01T01:05:00Z",
                        "data_status": "database",
                    }
                ]
            },
        )

    def test_default_database_provider_is_lazy(self):
        calls = []

        def connect(database_url):
            calls.append(database_url)
            raise AssertionError("connection must not be opened during app creation")

        main.create_app(
            mode="database",
            database_url="server-side-configured-url",
            connection_factory=connect,
            health_tracer=lambda: True,
        )

        self.assertEqual(calls, [])

    def test_default_database_provider_closes_connection_on_success(self):
        row = (
            "DB-DEFAULT",
            "Default Battery",
            "VIC1",
            2.0,
            4.0,
            "registry",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            1,
            0,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        )
        calls = []

        class Cursor:
            def __init__(self):
                self.closed = False

            def execute(self, statement, parameters):
                self.parameters = parameters

            def fetchall(self):
                return [row]

            def close(self):
                self.closed = True

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()
                self.closed = False

            def cursor(self):
                return self.cursor_instance

            def close(self):
                self.closed = True

        connection = Connection()

        def connect(database_url):
            calls.append(database_url)
            return connection

        application = main.create_app(
            mode="database",
            database_url="server-side-configured-url",
            connection_factory=connect,
            health_tracer=lambda: True,
        )
        response = TestClient(application).get("/api/generators")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["generators"][0]["duid"], "DB-DEFAULT")
        self.assertEqual(calls, ["server-side-configured-url"])
        self.assertTrue(connection.cursor_instance.closed)
        self.assertTrue(connection.closed)

    def test_default_database_provider_closes_connection_on_query_failure(self):
        failure_marker = "default-query-marker"
        url_marker = "postgresql://default-secret.invalid/batterywatch"

        class Cursor:
            def __init__(self):
                self.closed = False

            def execute(self, statement, parameters):
                raise RuntimeError(f"{failure_marker}: {url_marker}")

            def close(self):
                self.closed = True

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()
                self.closed = False

            def cursor(self):
                return self.cursor_instance

            def close(self):
                self.closed = True

        connection = Connection()
        response = TestClient(
            main.create_app(
                mode="database",
                database_url=url_marker,
                connection_factory=lambda _: connection,
                health_tracer=lambda: True,
            )
        ).get("/api/generators")

        self.assertEqual(
            response.json(), {"detail": "Database generators unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(url_marker, response.text)
        self.assertNotIn("BWTEST1", response.text)
        self.assertTrue(connection.cursor_instance.closed)
        self.assertTrue(connection.closed)

    def test_database_generators_returns_generic_503_for_provider_failure(self):
        failure_marker = "provider-failure-marker"
        url_marker = "postgresql://secret-marker.invalid/batterywatch"

        def provider():
            raise RuntimeError(f"{failure_marker}: {url_marker}")

        response = TestClient(
            main.create_app(
                mode="database",
                database_url=url_marker,
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get("/api/generators")

        self.assertEqual(
            response.json(), {"detail": "Database generators unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(url_marker, response.text)
        self.assertNotIn("BWTEST1", response.text)

    def test_database_generators_returns_generic_503_for_context_entry_failure(self):
        failure_marker = "context-entry-marker"
        url_marker = "postgresql://entry-secret.invalid/batterywatch"

        class FailingContext:
            def __enter__(self):
                raise RuntimeError(f"{failure_marker}: {url_marker}")

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        response = TestClient(
            main.create_app(
                mode="database",
                database_url=url_marker,
                health_tracer=lambda: True,
                repository_provider=lambda: FailingContext(),
            )
        ).get("/api/generators")

        self.assertEqual(
            response.json(), {"detail": "Database generators unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(url_marker, response.text)
        self.assertNotIn("BWTEST1", response.text)

    def test_database_generators_returns_generic_503_for_list_failure(self):
        failure_marker = "list-query-marker"
        url_marker = "postgresql://query-secret.invalid/batterywatch"

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def list_generators(self):
                    raise RuntimeError(f"{failure_marker}: {url_marker}")

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                database_url=url_marker,
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get("/api/generators")

        self.assertEqual(
            response.json(), {"detail": "Database generators unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(url_marker, response.text)
        self.assertNotIn("BWTEST1", response.text)

    def test_database_generators_returns_generic_503_for_mapping_failure(self):
        failure_marker = "mapping-failure-marker"
        url_marker = "postgresql://mapping-secret.invalid/batterywatch"

        class InvalidRecord:
            generator_id = "DB-BAD"
            site_name = "Bad Battery"
            region = "NSW1"
            capacity_mw = 1.0
            storage_capacity_mwh = 2.0
            @property
            def data_start(self):
                raise RuntimeError(f"{failure_marker}: {url_marker}")

            data_end = None

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def list_generators(self) -> tuple[GeneratorMetadata, ...]:
                    return (InvalidRecord(),)  # type: ignore[reportReturnType]

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                database_url=url_marker,
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get("/api/generators")

        self.assertEqual(
            response.json(), {"detail": "Database generators unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(url_marker, response.text)
        self.assertNotIn("BWTEST1", response.text)

    def test_database_generators_returns_generic_503_for_context_exit_failure(self):
        failure_marker = "context-exit-marker"
        url_marker = "postgresql://exit-secret.invalid/batterywatch"

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def list_generators(self):
                    return ()

            try:
                yield Repository()
            finally:
                raise RuntimeError(f"{failure_marker}: {url_marker}")

        response = TestClient(
            main.create_app(
                mode="database",
                database_url=url_marker,
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get("/api/generators")

        self.assertEqual(
            response.json(), {"detail": "Database generators unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(url_marker, response.text)
        self.assertNotIn("BWTEST1", response.text)

    def test_database_generators_returns_null_for_unavailable_bounds(self):
        record = GeneratorMetadata(
            generator_id="DB-2",
            site_name="Partial Battery",
            region="NSW1",
            capacity_mw=1.0,
            storage_capacity_mwh=2.0,
            source_id="registry",
            source_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ingestion_version=1,
            data_start=None,
            data_end=None,
        )

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def list_generators(self):
                    return (record,)

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get("/api/generators")

        self.assertEqual(response.status_code, 200)
        generator = response.json()["generators"][0]
        self.assertIsNone(generator["data_start"])
        self.assertIsNone(generator["data_end"])

    def test_database_generators_uses_injected_provider(self):
        events = []

        class Repository(InMemoryRepository):
            def list_generators(self):
                events.append("list")
                return ()

        @contextmanager
        def provider():
            events.append("enter")
            yield Repository()
            events.append("exit")

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get("/api/generators")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"generators": []})
        self.assertEqual(events, ["enter", "list", "exit"])

    def test_fixture_generators_do_not_invoke_a_database_provider(self):
        calls = []

        def provider():
            calls.append("called")
            raise AssertionError("fixture mode must not use the database provider")

        response = TestClient(
            main.create_app(repository_provider=provider)
        ).get("/api/generators")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["generators"][0]["duid"], "BWTEST1")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
