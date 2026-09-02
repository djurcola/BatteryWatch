"""Focused database FCAS API contract tests."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from typing import cast

from fastapi.testclient import TestClient

import batterywatch_api.main as main
from batterywatch_api.storage import (
    FCAS_SERVICES,
    FcasService5m,
    GeneratorFcas5m,
    GeneratorMetadata,
    InMemoryRepository,
    PostgreSQLConnection,
    PostgreSQLRepository,
    StorageRepository,
)

UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
SERVICES = (
    "raise_1s",
    "lower_1s",
    "raise_6s",
    "lower_6s",
    "raise_60s",
    "lower_60s",
    "raise_5m",
    "lower_5m",
    "raise_reg",
    "lower_reg",
)


def _metadata() -> GeneratorMetadata:
    return GeneratorMetadata(
        generator_id="DB-FCAS",
        site_name="FCAS Battery",
        region="NSW1",
        capacity_mw=2.0,
        storage_capacity_mwh=4.0,
        source_id="registry",
        source_timestamp=START,
        ingestion_version=1,
    )


def _fcas_row(
    interval_start: datetime = START,
    *,
    changes: dict[str, tuple[float | None, int | None, float | None]] | None = None,
) -> GeneratorFcas5m:
    changed = changes or {
        "raise_6s": (0.0, 1, 0.0),
    }
    values = {
        service: FcasService5m(
            target_mw=None,
            enablement_status=None,
            actual_availability_mw=None,
        )
        for service in SERVICES
    }
    for service, value in changed.items():
        values[service] = FcasService5m(*value)
    return GeneratorFcas5m(
        generator_id="DB-FCAS",
        interval_start=interval_start,
        services=values,
        last_changed=interval_start,
        report_timestamp=interval_start + timedelta(hours=1),
        downloaded_at=interval_start + timedelta(hours=2),
        intervention=0,
        run_number=1,
        dispatch_interval="20260101001",
        ingestion_version=1,
        correction_version=0,
        source_artifact_sha256="a" * 64,
    )


class DatabaseFcasApiTests(unittest.TestCase):
    def test_storage_repository_declares_the_bounded_fcas_read(self):
        method = getattr(StorageRepository, "list_fcas", None)
        self.assertTrue(callable(method))

    def _client(
        self,
        rows=(),
        *,
        clock=lambda: datetime(2026, 1, 1, 12, tzinfo=UTC),
        read_generator=None,
        list_fcas=None,
    ):
        metadata = _metadata()

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return metadata if read_generator is None else read_generator(generator_id)

                def list_fcas(self, generator_id, start=None, end=None):
                    if list_fcas is not None:
                        return list_fcas(generator_id, start, end)
                    return tuple(rows)

            yield Repository()

        return TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
                clock=clock,
            )
        )

    def test_grouped_point_preserves_null_and_explicit_zero_service_values(self):
        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return _metadata()

                def list_fcas(self, generator_id, start=None, end=None):
                    return (_fcas_row(),)

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": START.isoformat().replace("+00:00", "Z"),
                "end": END.isoformat().replace("+00:00", "Z"),
                "services": "raise_1s,raise_6s",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["selected_services"], ["raise_1s", "raise_6s"])
        self.assertEqual(len(body["points"]), 1)
        self.assertEqual(
            set(body["points"][0]["services"]),
            {"raise_1s", "raise_6s"},
        )
        self.assertIsNone(
            body["points"][0]["services"]["raise_1s"]["target_mw"]
        )
        self.assertEqual(
            body["points"][0]["services"]["raise_6s"]["target_mw"],
            0.0,
        )
        self.assertEqual(
            body["points"][0]["services"]["raise_6s"]["actual_availability_mw"],
            0.0,
        )
        self.assertFalse(body["points"][0]["services"]["raise_6s"]["response_verified"])

    def test_range_and_service_filter_are_validated_before_storage_reads(self):
        calls = []

        def list_fcas(generator_id, start, end):
            calls.append((generator_id, start, end))
            return ()

        client = self._client(list_fcas=list_fcas)
        base = {"generator": "DB-FCAS"}
        invalid_requests = (
            base,
            base | {"start": "2026-01-01T00:00:00", "end": "2026-01-01T00:05:00Z"},
            base | {"start": "2026-01-01T00:05:00Z", "end": "2026-01-01T00:00:00Z"},
            base
            | {
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-31T00:00:01Z",
            },
        )
        for params in invalid_requests:
            with self.subTest(params=params):
                self.assertEqual(client.get("/api/fcas", params=params).status_code, 400)
        for service_filter in ("", "raise_1s,raise_1s", "unknown", "raise_1s, raise_6s"):
            with self.subTest(service_filter=service_filter):
                response = client.get(
                    "/api/fcas",
                    params=base
                    | {
                        "start": "2026-01-01T00:00:00Z",
                        "end": "2026-01-01T00:05:00Z",
                        "services": service_filter,
                    },
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(calls, [])

    def test_exact_thirty_day_range_is_allowed_and_one_second_over_is_rejected(self):
        client = self._client()
        exact = client.get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-31T00:00:00Z",
            },
        )
        self.assertEqual(exact.status_code, 200)
        over = client.get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-31T00:00:01Z",
            },
        )
        self.assertEqual(over.status_code, 400)

    def test_unknown_generator_is_checked_before_fcas_rows(self):
        calls = []

        def read_generator(generator_id):
            calls.append(("generator", generator_id))
            return None

        def list_fcas(generator_id, start, end):
            calls.append(("fcas", generator_id, start, end))
            return (_fcas_row(),)

        response = self._client(
            read_generator=read_generator,
            list_fcas=list_fcas,
        ).get(
            "/api/fcas",
            params={
                "generator": "NOPE",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(calls, [("generator", "NOPE")])

    def test_default_response_uses_all_canonical_services_in_canonical_order(self):
        body = self._client(rows=(_fcas_row(),)).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        ).json()
        self.assertEqual(body["selected_services"], list(FCAS_SERVICES))
        self.assertEqual(set(body["points"][0]["services"]), set(FCAS_SERVICES))
        self.assertEqual(set(body["service_summaries"]), set(FCAS_SERVICES))

    def test_classification_preserves_enabled_trapped_stranded_and_epsilon_states(self):
        row = _fcas_row(
            changes={
                "raise_1s": (2.0, 1, 0.0),
                "lower_1s": (2.0, 3, 0.0),
                "raise_6s": (2.0, 4, 0.0),
                "lower_6s": (0.000001, 1, 0.0),
            }
        )
        body = self._client(rows=(row,)).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
                "services": "raise_1s,lower_1s,raise_6s,lower_6s",
            },
        ).json()
        values = body["points"][0]["services"]
        self.assertTrue(values["raise_1s"]["enabled"])
        self.assertTrue(values["raise_1s"]["cleared"])
        self.assertTrue(values["raise_1s"]["participating"])
        self.assertTrue(values["lower_1s"]["enabled"])
        self.assertTrue(values["lower_1s"]["trapped"])
        self.assertTrue(values["lower_1s"]["participating"])
        self.assertFalse(values["raise_6s"]["enabled"])
        self.assertTrue(values["raise_6s"]["stranded"])
        self.assertTrue(values["raise_6s"]["cleared"])
        self.assertFalse(values["raise_6s"]["participating"])
        self.assertFalse(values["lower_6s"]["cleared"])
        self.assertFalse(values["lower_6s"]["participating"])
        self.assertFalse(values["raise_6s"]["response_verified"])

    def test_service_summaries_include_enabled_counts_and_availability_maximum(self):
        rows = (
            _fcas_row(
                START,
                changes={
                    "raise_1s": (2.0, 1, 1.5),
                },
            ),
            _fcas_row(
                START + timedelta(minutes=5),
                changes={
                    "raise_1s": (1.0, 3, 2.5),
                },
            ),
        )
        body = self._client(rows=rows).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:10:00Z",
                "services": "raise_1s",
            },
        ).json()

        self.assertEqual(
            body["service_summaries"]["raise_1s"]["enabled_intervals"],
            2,
        )
        self.assertEqual(
            body["service_summaries"]["raise_1s"]["max_actual_availability_mw"],
            2.5,
        )

    def test_filtering_keeps_grouped_intervals_and_summaries(self):
        rows = (
            _fcas_row(
                START,
                changes={
                    "raise_1s": (None, None, None),
                    "raise_6s": (0.0, 1, 0.0),
                    "lower_6s": (2.0, 3, 0.0),
                },
            ),
            _fcas_row(
                START + timedelta(minutes=10),
                changes={
                    "raise_1s": (1.0, 4, 0.0),
                    "raise_6s": (3.0, 1, 0.0),
                },
            ),
        )
        calls = []

        def list_fcas(generator_id, start, end):
            calls.append((generator_id, start, end))
            return rows

        response = self._client(
            list_fcas=list_fcas,
            clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        ).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:15:00Z",
                "services": "raise_1s,raise_6s",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["selected_services"], ["raise_1s", "raise_6s"])
        self.assertEqual(len(body["points"]), 3)
        self.assertEqual(
            [point["timestamp"] for point in body["points"]],
            [
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:05:00Z",
                "2026-01-01T00:10:00Z",
            ],
        )
        self.assertEqual(
            body["points"][0]["services"]["raise_6s"]["target_mw"], 0.0
        )
        self.assertFalse(body["points"][0]["services"]["raise_6s"]["cleared"])
        self.assertIsNone(
            body["points"][1]["services"]["raise_6s"]["target_mw"]
        )
        self.assertEqual(
            body["points"][2]["services"]["raise_1s"]["target_mw"], 1.0
        )
        self.assertTrue(body["points"][2]["services"]["raise_1s"]["cleared"])
        self.assertTrue(body["points"][2]["services"]["raise_1s"]["stranded"])
        self.assertFalse(body["points"][2]["services"]["raise_1s"]["participating"])
        self.assertEqual(body["coverage"]["expected_intervals"], 3)
        self.assertEqual(body["coverage"]["observed_intervals"], 2)
        self.assertEqual(body["coverage"]["missing_intervals"], 1)
        self.assertAlmostEqual(body["coverage"]["coverage_percent"], 200 / 3)
        self.assertEqual(
            body["service_summaries"]["raise_1s"],
            {
                "reported_intervals": 1,
                "enabled_intervals": 0,
                "cleared_intervals": 1,
                "participating_intervals": 0,
                "trapped_intervals": 0,
                "stranded_intervals": 1,
                "max_target_mw": 1.0,
                "max_actual_availability_mw": 0.0,
            },
        )
        self.assertEqual(
            body["service_summaries"]["raise_6s"],
            {
                "reported_intervals": 2,
                "enabled_intervals": 2,
                "cleared_intervals": 1,
                "participating_intervals": 1,
                "trapped_intervals": 0,
                "stranded_intervals": 0,
                "max_target_mw": 3.0,
                "max_actual_availability_mw": 0.0,
            },
        )
        self.assertEqual(
            body["latest_finalized"]["interval_start"],
            "2026-01-01T00:10:00Z",
        )
        self.assertEqual(body["publication_state"], "partial")
        self.assertEqual(calls, [("DB-FCAS", START, START + timedelta(minutes=15))])

    def test_mixed_window_with_only_current_nem_day_gap_is_not_yet_public(self):
        row = _fcas_row(datetime(2026, 1, 1, 13, 55, tzinfo=UTC))
        response = self._client(
            rows=(row,),
            clock=lambda: datetime(2026, 1, 1, 14, 5, tzinfo=UTC),
        ).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T13:55:00Z",
                "end": "2026-01-01T14:05:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["coverage"]["expected_intervals"], 2)
        self.assertEqual(body["coverage"]["observed_intervals"], 1)
        self.assertEqual(body["coverage"]["missing_intervals"], 1)
        self.assertEqual(body["publication_state"], "not_yet_public")

    def test_mixed_historical_and_current_nem_day_gaps_remain_partial(self):
        row = _fcas_row(datetime(2026, 1, 1, 13, 50, tzinfo=UTC))
        response = self._client(
            rows=(row,),
            clock=lambda: datetime(2026, 1, 1, 14, 5, tzinfo=UTC),
        ).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T13:50:00Z",
                "end": "2026-01-01T14:05:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["coverage"]["missing_intervals"], 2)
        self.assertEqual(body["publication_state"], "partial")

    def test_current_day_and_historical_empty_windows_have_honest_publication_states(self):
        current_clock = lambda: datetime(2026, 1, 1, 12, tzinfo=UTC)
        current = self._client(clock=current_clock).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["publication_state"], "not_yet_public")
        self.assertIsNone(
            current.json()["points"][0]["services"]["raise_1s"]["target_mw"]
        )
        before_nem_midnight = self._client(
            clock=lambda: datetime(2026, 1, 1, 13, 59, tzinfo=UTC)
        ).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T13:55:00Z",
                "end": "2026-01-01T14:00:00Z",
            },
        )
        self.assertEqual(
            before_nem_midnight.json()["publication_state"],
            "not_yet_public",
        )
        after_nem_midnight = self._client(
            clock=lambda: datetime(2026, 1, 1, 14, tzinfo=UTC)
        ).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T13:55:00Z",
                "end": "2026-01-01T14:00:00Z",
            },
        )
        self.assertEqual(
            after_nem_midnight.json()["publication_state"],
            "no_data",
        )
        historical = self._client(clock=current_clock).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2025-12-31T00:00:00Z",
                "end": "2025-12-31T00:05:00Z",
            },
        )
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(historical.json()["publication_state"], "no_data")
        self.assertNotIn("inactive", current.text.lower())
        self.assertNotIn("inactive", historical.text.lower())

    def test_storage_and_mapping_failures_are_generic_503s(self):
        failure_marker = "fcas-storage-marker"
        secret_marker = "postgresql://fcas-secret.invalid/batterywatch"

        def failed_list(generator_id, start, end):
            raise RuntimeError(f"{failure_marker}: {secret_marker}")

        storage_response = self._client(list_fcas=failed_list).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )
        self.assertEqual(storage_response.status_code, 503)
        self.assertEqual(storage_response.json(), {"detail": "Database FCAS unavailable"})
        self.assertNotIn(failure_marker, storage_response.text)
        self.assertNotIn(secret_marker, storage_response.text)

        bad_row = SimpleNamespace(interval_start=START, services={})
        mapping_response = self._client(list_fcas=lambda *_: (bad_row,)).get(
            "/api/fcas",
            params={
                "generator": "DB-FCAS",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )
        self.assertEqual(mapping_response.status_code, 503)
        self.assertEqual(mapping_response.json(), {"detail": "Database FCAS unavailable"})

    def test_fixture_mode_does_not_fallback_to_fixture_fcas(self):
        calls = []

        def provider():
            calls.append("called")
            raise AssertionError("fixture FCAS must not use a database provider")

        response = TestClient(
            main.create_app(repository_provider=provider)
        ).get(
            "/api/fcas",
            params={
                "generator": "BWTEST1",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, [])


class PostgreSQLFcasStorageTests(unittest.TestCase):
    def test_list_fcas_reads_only_bounded_effective_grouped_rows(self):
        service_map: dict[str, dict[str, float | int | None]] = {
            service: {
                "target_mw": None,
                "enablement_status": None,
                "actual_availability_mw": None,
            }
            for service in FCAS_SERVICES
        }
        service_map["raise_6s"] = {
            "target_mw": 0.0,
            "enablement_status": 1,
            "actual_availability_mw": 0.0,
        }
        row = (
            "DB-FCAS",
            START,
            service_map,
            START,
            START + timedelta(hours=1),
            START + timedelta(hours=2),
            0,
            1,
            "20260101001",
            1,
            0,
            "a" * 64,
        )

        class Cursor:
            def __init__(self, connection):
                self.connection = connection
                self.rows = []

            def execute(self, statement, parameters):
                self.connection.execution = (statement, tuple(parameters))
                self.rows = [row]

            def fetchall(self):
                return self.rows

            def close(self):
                self.connection.closed = True

        class Connection:
            def __init__(self):
                self.closed = False
                self.execution: tuple[str, tuple[object, ...]] | None = None

            def cursor(self):
                return Cursor(self)

        connection = Connection()
        values = PostgreSQLRepository(cast(PostgreSQLConnection, connection)).list_fcas(
            "DB-FCAS", start=START, end=END
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].services["raise_6s"].target_mw, 0.0)
        self.assertIsNone(values[0].services["raise_1s"].target_mw)
        self.assertIsNotNone(connection.execution)
        assert connection.execution is not None
        statement, parameters = connection.execution
        self.assertIn("FROM generator_fcas_5m", statement)
        self.assertNotIn("FROM raw_nextday_fcas_observations", statement)
        self.assertIn("interval_start < %s::timestamptz", statement)
        self.assertEqual(parameters, ("DB-FCAS", START, START, END, END))
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
