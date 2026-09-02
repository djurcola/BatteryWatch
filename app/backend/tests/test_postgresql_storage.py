"""Focused fake-connection tests for the S2b PostgreSQL storage adapter."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from batterywatch_api.storage import (
    GeneratorMetadata,
    GeneratorPower5m,
    GeneratorSoc5m,
    PostgreSQLRepository,
    RegionalPrice5m,
)


UTC = timezone.utc
INTERVAL = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
SOURCE_TIME = datetime(2026, 1, 1, 0, 5, 3, tzinfo=UTC)
END = datetime(2026, 1, 1, 1, 5, tzinfo=UTC)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, statement, parameters):
        self.connection.executions.append((statement, tuple(parameters)))
        if self.connection.error is not None:
            raise self.connection.error
        self.rows = list(self.connection.responses.pop(0)) if self.connection.responses else []

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(self, responses=(), error=None):
        self.responses = list(responses)
        self.error = error
        self.executions = []
        self.commits = 0
        self.rollbacks = 0
        self.closed_cursors = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def generator_record(**overrides):
    values = {
        "generator_id": "BAT-1",
        "site_name": "Test Battery",
        "region": "NSW1",
        "capacity_mw": 2.0,
        "storage_capacity_mwh": 4.0,
        "source_id": "registry-v1",
        "source_timestamp": SOURCE_TIME,
        "ingestion_version": 3,
        "correction_version": 1,
        "data_start": INTERVAL,
        "data_end": END,
    }
    values.update(overrides)
    return GeneratorMetadata(**values)


def power_record(**overrides):
    values = {
        "generator_id": "BAT-1",
        "interval_start": INTERVAL,
        "power_mw": 0.0,
        "source_id": "nem-dispatch",
        "source_timestamp": SOURCE_TIME,
        "ingestion_version": 1,
        "correction_version": 0,
    }
    values.update(overrides)
    return GeneratorPower5m(**values)


def soc_record(**overrides):
    values = {
        "generator_id": "BAT-1",
        "interval_start": INTERVAL,
        "soc_percent": None,
        "source_id": "battery-telemetry",
        "source_timestamp": SOURCE_TIME,
        "ingestion_version": 1,
        "correction_version": 0,
        "quality_flags": ("unavailable",),
    }
    values.update(overrides)
    return GeneratorSoc5m(**values)


def price_record(**overrides):
    values = {
        "region": "NSW1",
        "interval_start": INTERVAL,
        "price_aud_per_mwh": None,
        "price_status": "missing",
        "source_id": "nem-rrp",
        "source_timestamp": SOURCE_TIME,
        "ingestion_version": 1,
        "correction_version": 0,
        "quality_flags": ("unavailable",),
    }
    values.update(overrides)
    return RegionalPrice5m(**values)


class PostgreSQLRepositoryWriteTests(unittest.TestCase):
    def test_generator_upsert_uses_parameterized_bindings(self):
        connection = FakeConnection(responses=([("applied",)],))
        repository = PostgreSQLRepository(connection)
        record = generator_record()

        self.assertTrue(repository.upsert_generator(record))
        self.assertEqual(connection.commits, 1)
        statement, parameters = connection.executions[0]
        self.assertIn("INSERT INTO generators", statement)
        self.assertIn("ON CONFLICT (generator_id)", statement)
        self.assertIn("%s", statement)
        self.assertNotIn(record.generator_id, statement)
        self.assertNotIn(record.site_name, statement)
        self.assertEqual(
            parameters,
            (
                "BAT-1",
                "Test Battery",
                "NSW1",
                2.0,
                4.0,
                INTERVAL,
                END,
                "registry-v1",
                SOURCE_TIME,
                3,
                1,
            ),
        )

    def test_measurement_upserts_bind_zero_unavailable_values(self):
        connection = FakeConnection(
            responses=([("applied",)], [
                ("applied",),
            ], [
                ("applied",),
            ])
        )
        repository = PostgreSQLRepository(connection)

        power = power_record()
        soc = soc_record()
        price = price_record()
        self.assertTrue(repository.upsert_power(power))
        self.assertTrue(repository.upsert_soc(soc))
        self.assertTrue(repository.upsert_price(price))

        power_sql, power_params = connection.executions[0]
        soc_sql, soc_params = connection.executions[1]
        price_sql, price_params = connection.executions[2]
        self.assertIn("INSERT INTO generator_power_5m", power_sql)
        self.assertIn("INSERT INTO generator_soc_5m", soc_sql)
        self.assertIn("INSERT INTO nem_price_5m", price_sql)
        self.assertEqual(power_params[2], 0.0)
        self.assertIsNone(soc_params[2])
        self.assertIsNone(price_params[2])
        self.assertEqual(price_params[3], "missing")
        for statement in (power_sql, soc_sql, price_sql):
            self.assertIn("%s", statement)
            self.assertNotIn("BAT-1", statement)
            self.assertNotIn("NSW1", statement)

    def test_price_upsert_binds_aemo_flags(self):
        connection = FakeConnection(responses=([("applied",)],))
        repository = PostgreSQLRepository(connection)
        record = price_record(
            price_aud_per_mwh=12.5,
            price_status="available",
            intervention=1,
            apc_flag=4,
            market_suspended=True,
        )

        self.assertTrue(repository.upsert_price(record))
        statement, parameters = connection.executions[0]
        self.assertIn("intervention", statement)
        self.assertIn("apc_flag", statement)
        self.assertIn("market_suspended", statement)
        self.assertIn("intervention = EXCLUDED.intervention", statement)
        self.assertIn("apc_flag = EXCLUDED.apc_flag", statement)
        self.assertIn("market_suspended = EXCLUDED.market_suspended", statement)
        self.assertEqual(
            parameters,
            (
                "NSW1",
                INTERVAL,
                12.5,
                "available",
                1,
                4,
                True,
                "nem-rrp",
                SOURCE_TIME,
                1,
                0,
                ["unavailable"],
            ),
        )

    def test_returning_row_distinguishes_replay_from_newer_correction(self):
        connection = FakeConnection(
            responses=([("applied",)], [], [("applied",)])
        )
        repository = PostgreSQLRepository(connection)
        original = power_record(power_mw=1.25, ingestion_version=1)
        replay = power_record(power_mw=1.25, ingestion_version=1)
        corrected = power_record(
            power_mw=1.5,
            ingestion_version=2,
            correction_version=1,
            source_timestamp=SOURCE_TIME + timedelta(minutes=1),
        )

        self.assertTrue(repository.upsert_power(original))
        self.assertFalse(repository.upsert_power(replay))
        self.assertTrue(repository.upsert_power(corrected))
        self.assertEqual(connection.commits, 3)
        statement, _ = connection.executions[0]
        self.assertIn("WHERE (EXCLUDED.correction_version", statement)
        self.assertIn("EXCLUDED.source_timestamp", statement)

    def test_database_failure_rolls_back_propagates(self):
        failure = RuntimeError("database unavailable")
        connection = FakeConnection(error=failure)
        repository = PostgreSQLRepository(connection)

        with self.assertRaises(RuntimeError) as raised:
            repository.upsert_power(power_record())
        self.assertIs(raised.exception, failure)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.closed_cursors, 1)


class PostgreSQLRepositoryReadTests(unittest.TestCase):
    def test_reads_map_rows_preserve_zero_none_flags(self):
        connection = FakeConnection(
            responses=(
                [
                    (
                        "BAT-1",
                        "Test Battery",
                        "NSW1",
                        Decimal("2.0"),
                        Decimal("4.0"),
                        "registry-v1",
                        SOURCE_TIME,
                        3,
                        1,
                        INTERVAL,
                        END,
                    )
                ],
                [("BAT-1", INTERVAL, Decimal("0.0"), "nem-dispatch", SOURCE_TIME, 1, 0)],
                [("BAT-1", INTERVAL, None, "battery-telemetry", SOURCE_TIME, 1, 0, ["unavailable"])],
                [("NSW1", INTERVAL, Decimal("0.0"), "available", 0, 0, False, "nem-rrp", SOURCE_TIME, 1, 0, [])],
            )
        )
        repository = PostgreSQLRepository(connection)

        generator = repository.read_generator("BAT-1")
        power = repository.read_power("BAT-1", INTERVAL)
        soc = repository.read_soc("BAT-1", INTERVAL)
        price = repository.read_price("NSW1", INTERVAL)

        self.assertEqual(generator, generator_record())
        self.assertEqual(power, power_record())
        self.assertEqual(soc, soc_record())
        self.assertEqual(price, price_record(price_aud_per_mwh=0.0, price_status="available", quality_flags=()))
        self.assertIsNotNone(power)
        self.assertIsNotNone(soc)
        self.assertIsNotNone(price)
        assert power is not None
        assert soc is not None
        assert price is not None
        self.assertEqual(power.power_mw, 0.0)
        self.assertIsNone(soc.soc_percent)
        self.assertEqual(price.price_aud_per_mwh, 0.0)
        self.assertEqual(connection.closed_cursors, 4)

    def test_price_reads_map_aemo_flags(self):
        row = (
            "NSW1",
            INTERVAL,
            Decimal("12.5"),
            "available",
            1,
            4,
            True,
            "nem-rrp",
            SOURCE_TIME,
            1,
            0,
            [],
        )
        connection = FakeConnection(responses=([row], [row]))
        repository = PostgreSQLRepository(connection)

        try:
            point = repository.read_price("NSW1", INTERVAL)
            values = repository.list_prices(
                "NSW1", start=INTERVAL, end=INTERVAL + timedelta(minutes=5)
            )
        except Exception as exc:
            self.fail(f"price flag row mapping failed: {exc}")

        self.assertIsNotNone(point)
        assert point is not None
        expected = (1, 4, True)
        self.assertEqual(
            (point.intervention, point.apc_flag, point.market_suspended),
            expected,
        )
        self.assertEqual(
            [
                (item.intervention, item.apc_flag, item.market_suspended)
                for item in values
            ],
            [expected],
        )

    def test_missing_read_list_window_sorted(self):
        earlier = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        connection = FakeConnection(
            responses=(
                [],
                [
                    ("BAT-1", INTERVAL, 1.0, "nem-dispatch", SOURCE_TIME, 1, 0),
                    ("BAT-1", earlier, 0.5, "nem-dispatch", SOURCE_TIME, 1, 0),
                ],
            )
        )
        repository = PostgreSQLRepository(connection)
        start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone(timedelta(hours=10)))
        end = datetime(2026, 1, 1, 10, 10, tzinfo=timezone(timedelta(hours=10)))

        self.assertIsNone(repository.read_power("BAT-1", INTERVAL))
        values = repository.list_power("BAT-1", start=start, end=end)

        self.assertEqual([item.interval_start for item in values], [earlier, INTERVAL])
        statement, parameters = connection.executions[1]
        self.assertIn("ORDER BY interval_start ASC", statement)
        self.assertEqual(parameters, ("BAT-1", earlier, earlier, INTERVAL + timedelta(minutes=5), INTERVAL + timedelta(minutes=5)))

    def test_read_database_failure_is_not_silenced(self):
        failure = RuntimeError("query failed")
        connection = FakeConnection(error=failure)
        repository = PostgreSQLRepository(connection)

        with self.assertRaises(RuntimeError) as raised:
            repository.read_price("NSW1", INTERVAL)
        self.assertIs(raised.exception, failure)
        self.assertEqual(connection.closed_cursors, 1)


if __name__ == "__main__":
    unittest.main()