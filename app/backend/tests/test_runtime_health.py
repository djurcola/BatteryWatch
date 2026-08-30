"""Focused runtime-selection and health tracer tests."""

import inspect
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from batterywatch_api.database_main import create_database_application
import batterywatch_api.main as main


class _ProbeCursor:
    def __init__(self, error=None):
        self.statements = []
        self.error = error
        self.closed = False

    def execute(self, statement):
        self.statements.append(statement)
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


class _ProbeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class RuntimeHealthTests(unittest.TestCase):
    def test_main_exposes_application_factory(self):
        self.assertTrue(hasattr(main, "create_app"))

    def test_factory_returns_a_fresh_fixture_application(self):
        self.assertIsNot(main.create_app(), main.app)

    def test_factory_defaults_to_fixture_runtime_without_database_configuration(self):
        application = main.create_app()
        self.assertEqual(getattr(application.state, "data_mode", None), "fixture")

    def test_factory_accepts_an_explicit_runtime_mode(self):
        self.assertIn("mode", inspect.signature(main.create_app).parameters)

    def test_factory_accepts_data_mode_configuration(self):
        self.assertIn("data_mode", inspect.signature(main.create_app).parameters)

    def test_runtime_application_reads_explicit_database_mode_from_environment(self):
        application = main.create_runtime_app(
            {
                "BATTERYWATCH_DATA_MODE": "database",
                "BATTERYWATCH_DATABASE_URL": "postgresql://private",
            },
            health_tracer=lambda: True,
        )

        response = TestClient(application).get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data_mode"], "database")

    def test_database_entrypoint_cannot_be_switched_to_fixture_by_environment(self):
        application = create_database_application(
            {
                "BATTERYWATCH_DATA_MODE": "fixture",
                "BATTERYWATCH_DATABASE_URL": "postgresql://private",
            },
            health_tracer=lambda: True,
        )

        response = TestClient(application).get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data_mode"], "database")

    def test_database_mode_health_fails_closed_without_configuration(self):
        with patch.dict(
            os.environ,
            {"BATTERYWATCH_DATABASE_URL": "", "DATABASE_URL": ""},
        ):
            response = TestClient(main.create_app(mode="database")).get("/api/health")
        self.assertEqual(response.status_code, 503)

    def test_database_health_tracer_exposes_a_safe_check(self):
        tracer = main.DatabaseHealthTracer(None)
        self.assertFalse(getattr(tracer, "check", lambda: True)())

    def test_factory_accepts_an_injected_health_tracer(self):
        self.assertIn("health_tracer", inspect.signature(main.create_app).parameters)

    def test_database_health_uses_an_injected_healthy_tracer(self):
        def healthy_tracer():
            return True

        response = TestClient(
            main.create_app(mode="database", health_tracer=healthy_tracer)
        ).get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data_mode"], "database")

    def test_database_health_accepts_a_tracer_object(self):
        class HealthyTracer:
            def check(self):
                return True

        response = TestClient(
            main.create_app(mode="database", health_tracer=HealthyTracer())
        ).get("/api/health")
        self.assertEqual(response.status_code, 200)

    def test_factory_accepts_database_configuration(self):
        self.assertIn("database_url", inspect.signature(main.create_app).parameters)

    def test_factory_accepts_an_injected_connection_factory(self):
        self.assertIn("connection_factory", inspect.signature(main.create_app).parameters)

    def test_database_health_traces_an_injected_connection(self):
        calls = []

        class Cursor:
            def execute(self, statement):
                self.statement = statement

            def fetchone(self):
                return (1,)

            def close(self):
                pass

        class Connection:
            def __init__(self):
                self.closed = False

            def cursor(self):
                return Cursor()

            def close(self):
                self.closed = True

        def connect(database_url):
            calls.append(database_url)
            return Connection()

        response = TestClient(
            main.create_app(
                mode="database",
                database_url="configured",
                connection_factory=connect,
            )
        ).get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data_mode"], "database")
        self.assertEqual(calls, ["configured"])

    def test_database_health_returns_generic_503_when_psycopg_import_fails(self):
        import_marker = "driver-import-marker"
        url_marker = "postgresql://database-url-marker.invalid/batterywatch"
        application = main.create_app(
            mode="database", database_url=url_marker
        )

        with patch(
            "batterywatch_api.runtime.importlib.import_module",
            side_effect=ImportError(import_marker),
        ):
            response = TestClient(application).get("/api/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"detail": "Database health unavailable"}
        )
        self.assertNotIn("BWTEST1", response.text)
        self.assertNotIn(import_marker, response.text)
        self.assertNotIn(url_marker, response.text)

    def test_database_health_returns_generic_503_when_connection_factory_fails(self):
        failure_marker = "connection-factory-marker"
        url_marker = "postgresql://database-url-marker.invalid/batterywatch"

        def connect(database_url):
            raise RuntimeError(f"{failure_marker}: {database_url}")

        response = TestClient(
            main.create_app(
                mode="database",
                database_url=url_marker,
                connection_factory=connect,
            )
        ).get("/api/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"detail": "Database health unavailable"}
        )
        self.assertNotIn("BWTEST1", response.text)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(url_marker, response.text)

    def test_database_health_returns_generic_503_when_query_fails(self):
        failure_marker = "query-failure-marker"
        url_marker = "postgresql://database-url-marker.invalid/batterywatch"
        cursor = _ProbeCursor(
            error=RuntimeError(f"{failure_marker}: {url_marker}")
        )
        connection = _ProbeConnection(cursor)

        response = TestClient(
            main.create_app(
                mode="database",
                database_url=url_marker,
                connection_factory=lambda _: connection,
            )
        ).get("/api/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"detail": "Database health unavailable"}
        )
        self.assertNotIn("BWTEST1", response.text)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(url_marker, response.text)

    def test_database_health_probe_executes_exactly_select_one(self):
        cursor = _ProbeCursor()
        connection = _ProbeConnection(cursor)
        tracer = main.DatabaseHealthTracer(
            "database-url",
            connection_factory=lambda _: connection,
        )

        self.assertTrue(tracer.check())
        self.assertEqual(cursor.statements, ["SELECT 1"])

    def test_database_health_probe_closes_cursor_and_connection_on_success(self):
        cursor = _ProbeCursor()
        connection = _ProbeConnection(cursor)
        tracer = main.DatabaseHealthTracer(
            "database-url",
            connection_factory=lambda _: connection,
        )

        self.assertTrue(tracer.check())
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_database_health_probe_closes_cursor_and_connection_on_query_failure(self):
        cursor = _ProbeCursor(error=RuntimeError("query failure"))
        connection = _ProbeConnection(cursor)
        tracer = main.DatabaseHealthTracer(
            "database-url",
            connection_factory=lambda _: connection,
        )

        self.assertFalse(tracer.check())
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_database_mode_can_read_configuration_from_environment(self):
        calls = []

        class Cursor:
            def execute(self, statement):
                pass

            def close(self):
                pass

        class Connection:
            def cursor(self):
                return Cursor()

            def close(self):
                pass

        def connect(database_url):
            calls.append(database_url)
            return Connection()

        with patch.dict(
            os.environ, {"BATTERYWATCH_DATABASE_URL": "configured"}
        ):
            response = TestClient(
                main.create_app(mode="database", connection_factory=connect)
            ).get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["configured"])

    def test_factory_rejects_an_unknown_runtime_mode(self):
        with self.assertRaises(ValueError):
            main.create_app(mode="unsupported")

    def test_database_mode_does_not_fall_back_to_fixture_generators(self):
        response = TestClient(
            main.create_app(mode="database", health_tracer=lambda: True)
        ).get("/api/generators")
        self.assertEqual(response.status_code, 503)

    def test_database_mode_does_not_fall_back_to_fixture_series(self):
        response = TestClient(
            main.create_app(mode="database", health_tracer=lambda: True)
        ).get("/api/series", params={"generator": "BWTEST1"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
