"""Focused database-series tracer tests."""

from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta, timezone
import inspect
import unittest
from typing import cast

from fastapi.testclient import TestClient

import batterywatch_api.main as main
from batterywatch_api.storage import (
    GeneratorMetadata,
    GeneratorPower5m,
    GeneratorSoc5m,
    InMemoryRepository,
    RegionalPrice5m,
    StorageRepository,
)


class DatabaseSeriesApiTests(unittest.TestCase):
    def test_storage_repository_declares_bounded_series_list_methods(self):
        for name in ("list_power", "list_soc", "list_prices"):
            method = getattr(StorageRepository, name, None)
            self.assertTrue(callable(method), name)
            assert callable(method)
            parameters = list(inspect.signature(method).parameters)
            self.assertEqual(parameters[2:], ["start", "end"])
            self.assertIsNone(inspect.signature(method).parameters["start"].default)
            self.assertIsNone(inspect.signature(method).parameters["end"].default)

    def test_database_series_reads_generator_metadata_through_provider(self):
        record = GeneratorMetadata(
            generator_id="DB-1",
            site_name="Database Battery",
            region="QLD1",
            capacity_mw=3.5,
            storage_capacity_mwh=7.0,
            source_id="registry",
            source_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ingestion_version=1,
        )

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    self.read_generator_id = generator_id
                    return record

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-1",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)

    def test_database_series_unknown_generator_is_not_replaced_by_fixture(self):
        events = []

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    events.append(("read_generator", generator_id))
                    return None

                def list_power(self, generator_id, start=None, end=None):
                    raise AssertionError("unknown generators must not read series")

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "NOPE",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_database_series_list_failure_is_generic(self):
        failure_marker = "series-list-marker"
        secret_marker = "postgresql://list-secret.invalid/batterywatch"
        metadata = GeneratorMetadata(
            generator_id="DB-FAIL",
            site_name="Failure Battery",
            region="NSW1",
            capacity_mw=1.0,
            storage_capacity_mwh=2.0,
            source_id="registry",
            source_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ingestion_version=1,
        )

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return metadata

                def list_power(self, generator_id, start=None, end=None):
                    raise RuntimeError(f"{failure_marker}: {secret_marker}")

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-FAIL",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )

        self.assertEqual(
            response.json(), {"detail": "Database series unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(secret_marker, response.text)

    def test_database_series_mapping_failure_is_generic(self):
        failure_marker = "series-mapping-marker"
        secret_marker = "postgresql://mapping-secret.invalid/batterywatch"

        class InvalidMetadata:
            generator_id = "DB-BAD"
            site_name = "Bad Battery"
            region = "NSW1"
            capacity_mw = 1.0
            storage_capacity_mwh = 2.0

            @property
            def data_start(self):
                raise RuntimeError(f"{failure_marker}: {secret_marker}")

            data_end = None

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return cast(GeneratorMetadata, InvalidMetadata())

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-BAD",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )

        self.assertEqual(
            response.json(), {"detail": "Database series unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(secret_marker, response.text)

    def test_database_series_context_exit_failure_is_generic(self):
        failure_marker = "series-exit-marker"
        secret_marker = "postgresql://exit-secret.invalid/batterywatch"
        metadata = GeneratorMetadata(
            generator_id="DB-EXIT",
            site_name="Exit Battery",
            region="NSW1",
            capacity_mw=1.0,
            storage_capacity_mwh=2.0,
            source_id="registry",
            source_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ingestion_version=1,
        )

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return metadata

                def list_power(self, generator_id, start=None, end=None):
                    return ()

                def list_soc(self, generator_id, start=None, end=None):
                    return ()

                def list_prices(self, region, start=None, end=None):
                    return ()

            try:
                yield Repository()
            finally:
                raise RuntimeError(f"{failure_marker}: {secret_marker}")

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-EXIT",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )

        self.assertEqual(
            response.json(), {"detail": "Database series unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(secret_marker, response.text)

    def test_database_series_context_entry_failure_is_generic(self):
        failure_marker = "series-entry-marker"
        secret_marker = "postgresql://entry-secret.invalid/batterywatch"

        class FailingContext:
            def __enter__(self):
                raise RuntimeError(f"{failure_marker}: {secret_marker}")

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=lambda: FailingContext(),
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-1",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )

        self.assertEqual(
            response.json(), {"detail": "Database series unavailable"}
        )

    def test_database_series_provider_failure_is_generic_and_no_fixture_fallback(self):
        failure_marker = "series-provider-marker"
        secret_marker = "postgresql://series-secret.invalid/batterywatch"

        def provider():
            raise RuntimeError(f"{failure_marker}: {secret_marker}")

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "BWTEST1",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )

        self.assertEqual(
            response.json(), {"detail": "Database series unavailable"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(failure_marker, response.text)
        self.assertNotIn(secret_marker, response.text)
        self.assertNotIn("BWTEST1", response.text)

    def test_database_series_requires_explicit_start_and_end(self):
        provider_calls = []

        @contextmanager
        def provider():
            provider_calls.append("called")
            raise AssertionError("bounds must be validated before the provider")
            yield  # pragma: no cover

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get("/api/series", params={"generator": "DB-1"})

        self.assertEqual(response.status_code, 400)

    def test_database_series_rejects_non_increasing_bounds(self):
        def provider() -> AbstractContextManager[StorageRepository]:
            raise AssertionError("bounds must be validated before the provider")

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-UTC",
                "start": "2026-01-01T00:05:00Z",
                "end": "2026-01-01T00:05:00Z",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_database_series_rejects_ranges_over_thirty_days(self):
        def provider() -> AbstractContextManager[StorageRepository]:
            raise AssertionError("bounds must be validated before the provider")

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-UTC",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-31T00:00:01Z",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_database_series_accepts_exactly_thirty_days_and_rejects_one_second_over(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        metadata = GeneratorMetadata(
            generator_id="DB-30D",
            site_name="Thirty Day Battery",
            region="NSW1",
            capacity_mw=1.0,
            storage_capacity_mwh=2.0,
            source_id="registry",
            source_timestamp=start,
            ingestion_version=1,
        )

        @contextmanager
        def provider():
            repository = InMemoryRepository()
            repository.upsert_generator(metadata)
            yield repository

        client = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        )
        exact = client.get(
            "/api/series",
            params={
                "generator": "DB-30D",
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": (start + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
            },
        )
        self.assertEqual(exact.status_code, 200)

        def rejecting_provider() -> AbstractContextManager[StorageRepository]:
            raise AssertionError("over-limit bounds must be validated before the provider")

        over = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=rejecting_provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-30D",
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": (start + timedelta(days=30, seconds=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
            },
        )
        self.assertEqual(over.status_code, 400)
        self.assertEqual(over.json()["detail"], "Requested range exceeds the 30-day limit")

    def test_database_series_accepts_non_aligned_utc_query_bounds(self):
        metadata = GeneratorMetadata(
            generator_id="DB-NONALIGNED",
            site_name="Non-aligned Battery",
            region="NSW1",
            capacity_mw=1.0,
            storage_capacity_mwh=2.0,
            source_id="registry",
            source_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ingestion_version=1,
        )
        power = GeneratorPower5m(
            generator_id="DB-NONALIGNED",
            interval_start=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
            power_mw=0.0,
            source_id="dispatch",
            source_timestamp=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
            ingestion_version=1,
        )

        @contextmanager
        def provider():
            repository = InMemoryRepository()
            repository.upsert_generator(metadata)
            repository.upsert_power(power)
            yield repository

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-NONALIGNED",
                "start": "2026-01-01T00:01:00Z",
                "end": "2026-01-01T00:06:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["points"],
            [
                {
                    "timestamp": "2026-01-01T00:05:00Z",
                    "power_mw": 0.0,
                    "soc_percent": None,
                    "price_aud_per_mwh": None,
                    "energy_mwh": 0.0,
                    "gross_value_aud": None,
                    "charging_cost_aud": None,
                    "net_energy_value_aud": None,
                    "price_status": "missing",
                }
            ],
        )

    def test_database_series_reads_all_three_bounded_repository_series(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(minutes=20)
        metadata = GeneratorMetadata(
            generator_id="DB-BOUND",
            site_name="Bounded Battery",
            region="NSW1",
            capacity_mw=1.0,
            storage_capacity_mwh=2.0,
            source_id="registry",
            source_timestamp=start,
            ingestion_version=1,
        )
        power = GeneratorPower5m(
            generator_id="DB-BOUND",
            interval_start=start,
            power_mw=0.0,
            source_id="dispatch",
            source_timestamp=start,
            ingestion_version=1,
        )
        soc = GeneratorSoc5m(
            generator_id="DB-BOUND",
            interval_start=start,
            soc_percent=None,
            source_id="telemetry",
            source_timestamp=start,
            ingestion_version=1,
        )
        price = RegionalPrice5m(
            region="NSW1",
            interval_start=start,
            price_aud_per_mwh=None,
            price_status="missing",
            source_id="rrp",
            source_timestamp=start,
            ingestion_version=1,
        )
        calls = []

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return metadata

                def list_power(self, generator_id, start=None, end=None):
                    calls.append(("power", generator_id, start, end))
                    return (power,)

                def list_soc(self, generator_id, start=None, end=None):
                    calls.append(("soc", generator_id, start, end))
                    return (soc,)

                def list_prices(self, region, start=None, end=None):
                    calls.append(("prices", region, start, end))
                    return (price,)

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-BOUND",
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
            },
        )

        self.assertEqual(calls, [
            ("power", "DB-BOUND", start, end),
            ("soc", "DB-BOUND", start, end),
            ("prices", "NSW1", start, end),
        ])

    def test_database_series_uses_power_rows_with_exact_nullable_joins(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(minutes=15)
        metadata = GeneratorMetadata(
            generator_id="DB-JOIN",
            site_name="Join Battery",
            region="NSW1",
            capacity_mw=2.0,
            storage_capacity_mwh=4.0,
            source_id="registry",
            source_timestamp=start,
            ingestion_version=1,
        )
        power = tuple(
            GeneratorPower5m(
                generator_id="DB-JOIN",
                interval_start=start + timedelta(minutes=5 * index),
                power_mw=value,
                source_id="dispatch",
                source_timestamp=start,
                ingestion_version=1,
            )
            for index, value in enumerate((0.0, 2.0, -1.0))
        )
        soc = (
            GeneratorSoc5m(
                generator_id="DB-JOIN",
                interval_start=start,
                soc_percent=None,
                source_id="telemetry",
                source_timestamp=start,
                ingestion_version=1,
            ),
            GeneratorSoc5m(
                generator_id="DB-JOIN",
                interval_start=start + timedelta(minutes=5),
                soc_percent=55.0,
                source_id="telemetry",
                source_timestamp=start,
                ingestion_version=1,
            ),
        )
        prices = tuple(
            RegionalPrice5m(
                region="NSW1",
                interval_start=start + timedelta(minutes=5 * index),
                price_aud_per_mwh=value,
                price_status=(
                    "missing"
                    if value is None
                    else "negative"
                    if value < 0
                    else "available"
                ),
                source_id="rrp",
                source_timestamp=start,
                ingestion_version=1,
            )
            for index, value in enumerate((10.0, -20.0, None))
        )

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return metadata

                def list_power(self, generator_id, start=None, end=None):
                    return power

                def list_soc(self, generator_id, start=None, end=None):
                    return soc

                def list_prices(self, region, start=None, end=None):
                    return prices

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-JOIN",
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
            },
        )

        body = response.json()
        self.assertEqual(
            [point["timestamp"] for point in body["points"]],
            [
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:05:00Z",
                "2026-01-01T00:10:00Z",
            ],
        )
        self.assertEqual([point["power_mw"] for point in body["points"]], [0.0, 2.0, -1.0])
        self.assertIsNone(body["points"][0]["soc_percent"])
        self.assertEqual(body["points"][1]["soc_percent"], 55.0)
        self.assertIsNone(body["points"][2]["soc_percent"])
        self.assertEqual(body["points"][1]["price_status"], "negative")
        self.assertIsNone(body["points"][2]["price_aud_per_mwh"])
        self.assertEqual(body["points"][2]["price_status"], "missing")
        self.assertEqual(body["provenance"]["data_mode"], "database")
        self.assertEqual(body["provenance"]["power_source"], "dispatch")
        self.assertEqual(body["provenance"]["price_source"], "rrp")
        self.assertEqual(body["provenance"]["soc_source"], "telemetry")

    def test_database_series_emits_aligned_null_grid_and_gap_coverage(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(minutes=20)
        metadata = GeneratorMetadata(
            generator_id="DB-GAPS",
            site_name="Gap Battery",
            region="NSW1",
            capacity_mw=2.0,
            storage_capacity_mwh=4.0,
            source_id="registry",
            source_timestamp=start,
            ingestion_version=1,
        )
        power = tuple(
            GeneratorPower5m(
                generator_id="DB-GAPS",
                interval_start=start + timedelta(minutes=minutes),
                power_mw=value,
                source_id="dispatch",
                source_timestamp=start,
                ingestion_version=1,
            )
            for minutes, value in ((0, 0.0), (10, 2.0))
        )
        prices = tuple(
            RegionalPrice5m(
                region="NSW1",
                interval_start=start + timedelta(minutes=minutes),
                price_aud_per_mwh=value,
                price_status="missing" if value is None else "available",
                source_id="rrp",
                source_timestamp=start,
                ingestion_version=1,
            )
            for minutes, value in ((0, 100.0), (5, 80.0), (10, None))
        )

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return metadata

                def list_power(self, generator_id, start=None, end=None):
                    return power

                def list_soc(self, generator_id, start=None, end=None):
                    return ()

                def list_prices(self, region, start=None, end=None):
                    return prices

            yield Repository()

        body = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-GAPS",
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
            },
        ).json()

        self.assertEqual(
            [point["timestamp"] for point in body["points"]],
            [
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:05:00Z",
                "2026-01-01T00:10:00Z",
                "2026-01-01T00:15:00Z",
            ],
        )
        self.assertEqual([point["power_mw"] for point in body["points"]], [0.0, None, 2.0, None])
        self.assertEqual([point["price_aud_per_mwh"] for point in body["points"]], [100.0, 80.0, None, None])
        self.assertEqual(body["points"][0]["energy_mwh"], 0.0)
        for field in ("energy_mwh", "gross_value_aud", "charging_cost_aud", "net_energy_value_aud"):
            self.assertIsNone(body["points"][1][field])
            self.assertIsNone(body["points"][3][field])
        self.assertEqual(body["coverage"]["expected_intervals"], 4)
        self.assertEqual(body["coverage"]["observed_power_intervals"], 2)
        self.assertEqual(body["coverage"]["observed_price_intervals"], 2)
        self.assertEqual(body["coverage"]["missing_power_intervals"], 2)
        self.assertEqual(body["coverage"]["missing_price_intervals"], 2)
        self.assertEqual(body["coverage"]["both_missing_intervals"], 1)
        self.assertEqual(body["coverage"]["power_coverage_percent"], 50.0)
        self.assertEqual(body["coverage"]["price_coverage_percent"], 50.0)
        self.assertAlmostEqual(body["summary"]["total_energy_mwh"], 1 / 6)
        self.assertAlmostEqual(body["summary"]["exported_energy_mwh"], 1 / 6)
        self.assertEqual(body["summary"]["imported_energy_mwh"], 0.0)
        self.assertEqual(body["summary"]["gross_value_aud"], 0.0)
        self.assertEqual(body["summary"]["net_energy_value_aud"], 0.0)

    def test_database_series_reuses_estimate_math_and_coverage(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(minutes=15)
        metadata = GeneratorMetadata(
            generator_id="DB-ESTIMATE",
            site_name="Estimate Battery",
            region="NSW1",
            capacity_mw=2.0,
            storage_capacity_mwh=4.0,
            source_id="registry",
            source_timestamp=start,
            ingestion_version=1,
        )
        power = tuple(
            GeneratorPower5m(
                generator_id="DB-ESTIMATE",
                interval_start=start + timedelta(minutes=5 * index),
                power_mw=value,
                source_id="dispatch",
                source_timestamp=start,
                ingestion_version=1,
            )
            for index, value in enumerate((1.5, -1.0, 0.0))
        )
        soc = tuple(
            GeneratorSoc5m(
                generator_id="DB-ESTIMATE",
                interval_start=start + timedelta(minutes=5 * index),
                soc_percent=value,
                source_id="telemetry",
                source_timestamp=start,
                ingestion_version=1,
            )
            for index, value in enumerate((45.0, None, 50.0))
        )
        prices = tuple(
            RegionalPrice5m(
                region="NSW1",
                interval_start=start + timedelta(minutes=5 * index),
                price_aud_per_mwh=value,
                price_status="missing" if value is None else "available",
                source_id="rrp",
                source_timestamp=start,
                ingestion_version=1,
            )
            for index, value in enumerate((100.0, 80.0, None))
        )

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return metadata

                def list_power(self, generator_id, start=None, end=None):
                    return power

                def list_soc(self, generator_id, start=None, end=None):
                    return soc

                def list_prices(self, region, start=None, end=None):
                    return prices

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-ESTIMATE",
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
            },
        )

        body = response.json()
        self.assertAlmostEqual(body["points"][0]["energy_mwh"], 0.125)
        self.assertAlmostEqual(body["points"][0]["gross_value_aud"], 12.5)
        self.assertAlmostEqual(body["points"][1]["charging_cost_aud"], 80 / 12)
        self.assertIsNone(body["points"][2]["gross_value_aud"])
        self.assertEqual(body["coverage"]["missing_price_intervals"], 1)
        self.assertEqual(body["coverage"]["missing_soc_intervals"], 1)
        self.assertAlmostEqual(body["summary"]["exported_energy_mwh"], 0.125)
        self.assertAlmostEqual(body["summary"]["imported_energy_mwh"], 1 / 12)
        self.assertAlmostEqual(body["summary"]["net_energy_value_aud"], 12.5 - 80 / 12)

    def test_database_series_normalizes_offset_aware_bounds_to_utc(self):
        record = GeneratorMetadata(
            generator_id="DB-UTC",
            site_name="UTC Battery",
            region="NSW1",
            capacity_mw=1.0,
            storage_capacity_mwh=2.0,
            source_id="registry",
            source_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ingestion_version=1,
        )

        @contextmanager
        def provider():
            class Repository(InMemoryRepository):
                def read_generator(self, generator_id):
                    return record

            yield Repository()

        response = TestClient(
            main.create_app(
                mode="database",
                health_tracer=lambda: True,
                repository_provider=provider,
            )
        ).get(
            "/api/series",
            params={
                "generator": "DB-UTC",
                "start": "2026-01-01T10:00:00+10:00",
                "end": "2026-01-01T10:05:00+10:00",
            },
        )

        self.assertEqual(
            response.json()["requested_start"], "2026-01-01T00:00:00Z"
        )


if __name__ == "__main__":
    unittest.main()
