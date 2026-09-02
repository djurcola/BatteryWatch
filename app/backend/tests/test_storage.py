"""Persistence contract tests for the S2a storage seam."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from batterywatch_api.storage import (
    GeneratorMetadata,
    GeneratorPower5m,
    GeneratorSoc5m,
    InMemoryRepository,
    RegionalPrice5m,
)


UTC = timezone.utc
INTERVAL = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
SOURCE_TIME = datetime(2026, 1, 1, 0, 5, 3, tzinfo=UTC)


class StorageContractTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRepository()

    def test_records_normalize_utc_and_require_aligned_five_minute_intervals(self):
        offset = timezone(timedelta(hours=10))
        record = GeneratorPower5m(
            generator_id="BAT-1",
            interval_start=datetime(2026, 1, 1, 10, 5, tzinfo=offset),
            power_mw=0.0,
            source_id="nem-dispatch",
            source_timestamp=datetime(2026, 1, 1, 10, 5, 3, tzinfo=offset),
            ingestion_version=1,
        )

        self.assertEqual(record.interval_start, INTERVAL)
        self.assertEqual(record.interval_start.tzinfo, UTC)
        self.assertEqual(record.source_timestamp, SOURCE_TIME)
        with self.assertRaises(ValueError):
            GeneratorPower5m(
                generator_id="BAT-1",
                interval_start=datetime(2026, 1, 1, 0, 6, tzinfo=UTC),
                power_mw=1.0,
                source_id="nem-dispatch",
                source_timestamp=SOURCE_TIME,
                ingestion_version=1,
            )
        with self.assertRaises(ValueError):
            GeneratorPower5m(
                generator_id="BAT-1",
                interval_start=datetime(2026, 1, 1, 0, 5),
                power_mw=1.0,
                source_id="nem-dispatch",
                source_timestamp=SOURCE_TIME,
                ingestion_version=1,
            )

    def test_insert_and_read_back_preserves_zero_and_unavailable_values(self):
        generator = GeneratorMetadata(
            generator_id="BAT-1",
            site_name="Test Battery",
            region="NSW1",
            capacity_mw=2.0,
            storage_capacity_mwh=4.0,
            source_id="registry-v1",
            source_timestamp=SOURCE_TIME,
            ingestion_version=1,
        )
        power = GeneratorPower5m(
            generator_id="BAT-1",
            interval_start=INTERVAL,
            power_mw=0.0,
            source_id="nem-dispatch",
            source_timestamp=SOURCE_TIME,
            ingestion_version=1,
        )
        soc = GeneratorSoc5m(
            generator_id="BAT-1",
            interval_start=INTERVAL,
            soc_percent=None,
            source_id="battery-telemetry",
            source_timestamp=SOURCE_TIME,
            ingestion_version=1,
        )
        price = RegionalPrice5m(
            region="NSW1",
            interval_start=INTERVAL,
            price_aud_per_mwh=None,
            price_status="missing",
            source_id="nem-rrp",
            source_timestamp=SOURCE_TIME,
            ingestion_version=1,
            quality_flags=("unavailable",),
        )

        self.assertTrue(self.repository.insert_generator(generator))
        self.assertTrue(self.repository.insert_power(power))
        self.assertTrue(self.repository.insert_soc(soc))
        self.assertTrue(self.repository.insert_price(price))
        self.assertEqual(self.repository.read_generator("BAT-1"), generator)
        self.assertEqual(self.repository.read_power("BAT-1", INTERVAL), power)
        self.assertEqual(self.repository.read_soc("BAT-1", INTERVAL), soc)
        self.assertEqual(self.repository.read_price("NSW1", INTERVAL), price)
        read_power = self.repository.read_power("BAT-1", INTERVAL)
        read_soc = self.repository.read_soc("BAT-1", INTERVAL)
        read_price = self.repository.read_price("NSW1", INTERVAL)
        self.assertIsNotNone(read_power)
        self.assertIsNotNone(read_soc)
        self.assertIsNotNone(read_price)
        assert read_power is not None
        assert read_soc is not None
        assert read_price is not None
        self.assertEqual(read_power.power_mw, 0.0)
        self.assertIsNone(read_soc.soc_percent)
        self.assertIsNone(read_price.price_aud_per_mwh)
        self.assertTrue(read_price.is_missing)

    def test_replaying_source_record_is_idempotent(self):
        record = self._power(power_mw=1.25)

        self.assertTrue(self.repository.insert_power(record))
        self.assertFalse(self.repository.insert_power(record))
        self.assertEqual(self.repository.count_power("BAT-1"), 1)
        self.assertEqual(self.repository.list_power("BAT-1"), (record,))

    def test_later_correction_replaces_one_effective_interval_and_stale_data_does_not_regress(self):
        original = self._power(power_mw=1.25, ingestion_version=1, correction_version=0)
        corrected = self._power(
            power_mw=1.5,
            ingestion_version=2,
            correction_version=1,
            source_timestamp=SOURCE_TIME + timedelta(minutes=1),
        )
        stale = self._power(power_mw=0.5, ingestion_version=3, correction_version=0)

        self.assertTrue(self.repository.insert_power(original))
        self.assertTrue(self.repository.insert_power(corrected))
        self.assertFalse(self.repository.insert_power(stale))
        self.assertEqual(self.repository.count_power("BAT-1"), 1)
        self.assertEqual(self.repository.read_power("BAT-1", INTERVAL), corrected)

    def _power(self, *, power_mw, ingestion_version=1, correction_version=0, source_timestamp=SOURCE_TIME):
        return GeneratorPower5m(
            generator_id="BAT-1",
            interval_start=INTERVAL,
            power_mw=power_mw,
            source_id="nem-dispatch",
            source_timestamp=source_timestamp,
            ingestion_version=ingestion_version,
            correction_version=correction_version,
        )


class MigrationContractTests(unittest.TestCase):
    def test_initial_migration_declares_v1_five_minute_tables_and_guards(self):
        migration = Path(__file__).resolve().parents[2] / "migrations" / "001_initial_schema.sql"
        sql = migration.read_text(encoding="utf-8").lower()

        for table in (
            "generators",
            "generator_power_5m",
            "generator_soc_5m",
            "nem_price_5m",
        ):
            self.assertIn(f"create table if not exists {table}", sql)
        self.assertIn("timestamptz", sql)
        self.assertIn("primary key (generator_id, interval_start)", sql)
        self.assertIn("unique (generator_id, interval_start, source_id", sql)
        self.assertIn("correction_version", sql)
        self.assertIn("check", sql)
        self.assertIn("price_status", sql)
        self.assertIn("soc_percent", sql)
        self.assertIn("::text not in ('nan', 'infinity', '-infinity')", sql)
        self.assertIn("one effective record per logical key", sql)
        self.assertIn("revision history is not retained", sql)


if __name__ == "__main__":
    unittest.main()
