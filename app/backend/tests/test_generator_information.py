"""Contract tests for optional selected-generator information metadata."""

from datetime import datetime, timezone
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

import batterywatch_api.main as main
from batterywatch_api.storage import GeneratorMetadata, PostgreSQLRepository
from test_postgresql_storage import FakeConnection


OPTIONAL_FIELDS = (
    "asset_type",
    "owner_name",
    "technology_type",
    "fuel_type",
    "unit_status",
    "commercial_use_date",
    "battery_manufacturer",
    "battery_model",
    "inverter_manufacturer",
    "inverter_model",
    "dispatch_type",
    "status_bucket_summary",
    "fuel_bucket_summary",
)


class GeneratorInformationContractTests(unittest.TestCase):
    def test_migration_is_additive_idempotent_and_deployed_after_014(self):
        app_root = Path(__file__).resolve().parents[2]
        migration = (app_root / "migrations" / "015_generator_information.sql").read_text(
            encoding="utf-8"
        )
        deploy = (app_root / "deploy" / "migrate.sh").read_text(encoding="utf-8")

        for field in OPTIONAL_FIELDS:
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {field} TEXT", migration)
        self.assertNotIn("UPDATE GENERATORS", migration.upper())
        self.assertLess(
            deploy.index("014_fcas_validator_restore_safety.sql"),
            deploy.index("015_generator_information.sql"),
        )

    def test_fixture_and_database_api_expose_null_optional_metadata(self):
        expected = set(OPTIONAL_FIELDS) - {"owner_name"} | {"owner"}
        fixture = TestClient(main.create_app()).get("/api/generators")
        self.assertEqual(fixture.status_code, 200)
        self.assertEqual(expected, expected & set(fixture.json()["generators"][0]))
        for field in expected:
            self.assertIsNone(fixture.json()["generators"][0][field])

        record = GeneratorMetadata(
            generator_id="BAT-INFO",
            site_name="Information Battery",
            region="NSW1",
            capacity_mw=2.0,
            storage_capacity_mwh=4.0,
            source_id="registry",
            source_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ingestion_version=1,
            asset_type="Battery",
            owner_name="Owner Pty Ltd",
        )
        application = main.create_app(
            mode="database",
            health_tracer=lambda: True,
            repository_provider=lambda: _RepositoryContext(record),
        )
        response = TestClient(application).get("/api/generators")
        self.assertEqual(response.status_code, 200)
        generator = response.json()["generators"][0]
        self.assertEqual(generator["asset_type"], "Battery")
        self.assertEqual(generator["owner"], "Owner Pty Ltd")
        for field in expected - {"asset_type", "owner"}:
            self.assertIsNone(generator[field])

    def test_postgresql_maps_and_binds_optional_metadata_without_null_erasure(self):
        row = (
            "BAT-INFO", "Information Battery", "NSW1", 2.0, 4.0,
            "Battery", "Owner Pty Ltd", None, None, None, "Sep-25", None, None,
            None, None, None, None, None,
            "registry", datetime(2026, 1, 1, tzinfo=timezone.utc), 1, 0, None, None,
        )
        connection = FakeConnection(responses=([row], [("applied",)]))
        repository = PostgreSQLRepository(connection)
        record = repository.read_generator("BAT-INFO")
        assert record is not None
        self.assertEqual(record.owner_name, "Owner Pty Ltd")
        self.assertIsNone(record.technology_type)

        self.assertTrue(repository.upsert_generator(record))
        statement, parameters = connection.executions[1]
        self.assertIn("COALESCE(EXCLUDED.asset_type, generators.asset_type)", statement)
        self.assertIn("COALESCE(EXCLUDED.fuel_bucket_summary, generators.fuel_bucket_summary)", statement)
        self.assertEqual(parameters[7:20], row[5:18])


class _RepositoryContext:
    def __init__(self, record):
        self.record = record

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def list_generators(self):
        return (self.record,)


if __name__ == "__main__":
    unittest.main()
