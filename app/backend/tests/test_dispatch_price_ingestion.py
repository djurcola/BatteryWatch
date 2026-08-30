"""Tests for atomic official DispatchIS price ingestion."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from batterywatch_api.dispatch_price_ingestion import (
    DispatchPriceArtifactConflictError,
    DispatchPriceArtifactReceipt,
    DispatchPriceIngestionResult,
    PostgreSQLDispatchPriceIngestor,
)
from batterywatch_api.storage import RegionalPrice5m


UTC = timezone.utc
REPORT_TIMESTAMP = datetime(2026, 8, 30, 2, 5, tzinfo=UTC)
SOURCE_TIMESTAMP = REPORT_TIMESTAMP + timedelta(seconds=4)
INTERVAL = REPORT_TIMESTAMP
ARTIFACT_ID = "0000000000000042"
REGIONS = ("NSW1", "QLD1", "SA1", "TAS1", "VIC1")


class FakeCursor:
    def __init__(self, connection) -> None:
        self.connection = connection

    def execute(self, statement, parameters) -> None:
        self.connection.executions.append((statement, tuple(parameters)))
        if len(self.connection.executions) == self.connection.fail_on_execute:
            raise self.connection.failure

    def fetchone(self):
        if self.connection.fetchone_results:
            return self.connection.fetchone_results.pop(0)
        return (1,)

    def close(self) -> None:
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(self, *, fail_on_execute=None, failure=None, fetchone_results=()) -> None:
        self.executions = []
        self.fail_on_execute = fail_on_execute
        self.failure = failure
        self.fetchone_results = list(fetchone_results)
        self.cursor_calls = self.closed_cursors = 0
        self.commits = self.rollbacks = 0
        self._cursor = FakeCursor(self)

    def cursor(self):
        self.cursor_calls += 1
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def receipt() -> DispatchPriceArtifactReceipt:
    filename = f"PUBLIC_DISPATCHIS_202608301205_{ARTIFACT_ID}.zip"
    return DispatchPriceArtifactReceipt(
        source_artifact_id=ARTIFACT_ID,
        source_url=(
            "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
            + filename
        ),
        zip_filename=filename,
        csv_member_name=filename.removesuffix(".zip") + ".CSV",
        report_timestamp=REPORT_TIMESTAMP,
        zip_sha256="a" * 64,
        raw_zip=b"official-dispatchis-zip",
    )


def prices() -> tuple[RegionalPrice5m, ...]:
    return tuple(
        RegionalPrice5m(
            region=region,
            interval_start=INTERVAL,
            price_aud_per_mwh=-10.0 if region == "SA1" else float(index + 40),
            price_status="negative" if region == "SA1" else "available",
            intervention=0,
            apc_flag=0,
            market_suspended=False,
            source_id=ARTIFACT_ID,
            source_timestamp=SOURCE_TIMESTAMP,
            ingestion_version=int(ARTIFACT_ID),
            correction_version=0,
            quality_flags=(),
        )
        for index, region in enumerate(REGIONS)
    )


class PostgreSQLDispatchPriceIngestorTests(unittest.TestCase):
    def test_inserts_receipt_then_five_prices_in_one_commit(self) -> None:
        connection = FakeConnection()

        result = PostgreSQLDispatchPriceIngestor(connection).ingest(receipt(), prices())

        self.assertEqual(result, DispatchPriceIngestionResult(5, False))
        self.assertEqual(
            (connection.cursor_calls, connection.closed_cursors,
             connection.commits, connection.rollbacks),
            (1, 1, 1, 0),
        )
        self.assertEqual(len(connection.executions), 6)
        self.assertIn("dispatch_price_artifacts", connection.executions[0][0])
        self.assertTrue(all(
            "INSERT INTO nem_price_5m" in statement
            for statement, _ in connection.executions[1:]
        ))
        self.assertEqual(
            tuple(parameters[0] for _, parameters in connection.executions[1:]),
            REGIONS,
        )
        self.assertTrue(all("RETURNING 1" in statement for statement, _ in connection.executions))

    def test_exact_artifact_replay_is_a_clean_noop(self) -> None:
        artifact = receipt()
        stored = (
            artifact.source_artifact_id,
            artifact.source_url,
            artifact.zip_filename,
            artifact.csv_member_name,
            artifact.report_timestamp,
            artifact.zip_sha256,
            memoryview(artifact.raw_zip),
        )
        connection = FakeConnection(fetchone_results=(None, stored))

        result = PostgreSQLDispatchPriceIngestor(connection).ingest(artifact, prices())

        self.assertEqual(result, DispatchPriceIngestionResult(0, True))
        self.assertEqual(len(connection.executions), 2)
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))

    def test_conflicting_artifact_identity_rolls_back(self) -> None:
        artifact = receipt()
        stored = (
            artifact.source_artifact_id,
            artifact.source_url,
            artifact.zip_filename,
            artifact.csv_member_name,
            artifact.report_timestamp,
            "b" * 64,
            artifact.raw_zip,
        )
        connection = FakeConnection(fetchone_results=(None, stored))

        with self.assertRaises(DispatchPriceArtifactConflictError):
            PostgreSQLDispatchPriceIngestor(connection).ingest(artifact, prices())

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))

    def test_rejects_incomplete_region_set_before_database_write(self) -> None:
        connection = FakeConnection()

        with self.assertRaisesRegex(ValueError, "five canonical regions"):
            PostgreSQLDispatchPriceIngestor(connection).ingest(receipt(), prices()[:-1])

        self.assertEqual((connection.executions, connection.commits, connection.rollbacks), ([], 0, 1))

    def test_rejects_records_not_bound_to_artifact_interval_and_version(self) -> None:
        from dataclasses import replace

        mismatched_interval = tuple(
            replace(record, interval_start=INTERVAL + timedelta(minutes=5))
            for record in prices()
        )
        mismatched_version = tuple(
            replace(record, ingestion_version=1)
            for record in prices()
        )

        for records, message in (
            (mismatched_interval, "report timestamp"),
            (mismatched_version, "artifact version"),
        ):
            with self.subTest(message=message):
                connection = FakeConnection()
                with self.assertRaisesRegex(ValueError, message):
                    PostgreSQLDispatchPriceIngestor(connection).ingest(receipt(), records)
                self.assertEqual(connection.executions, [])

    def test_mid_batch_failure_rolls_back_and_reraises_same_error(self) -> None:
        failure = RuntimeError("price write failed")
        connection = FakeConnection(fail_on_execute=4, failure=failure)
        same_error = False

        try:
            PostgreSQLDispatchPriceIngestor(connection).ingest(receipt(), prices())
        except RuntimeError as raised:
            same_error = raised is failure
        else:
            self.fail("expected price write failure")

        self.assertTrue(same_error)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))


class DispatchPriceMigrationTests(unittest.TestCase):
    def test_backup_script_passes_connection_uri_explicitly_to_pg_dump(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = (root / "deploy" / "backup-verify.sh").read_text(encoding="utf-8")

        self.assertIn('database_url=$BATTERYWATCH_DATABASE_URL', script)
        self.assertIn('pg_dump --dbname="$database_url" --format=custom', script)
        self.assertNotIn('export PGDATABASE=', script)

    def test_deploy_migration_applies_and_verifies_price_artifact_table(self) -> None:
        app_root = Path(__file__).resolve().parents[2]
        migration = (app_root / "migrations" / "003_dispatch_price_artifacts.sql").read_text(
            encoding="utf-8"
        )
        migrate_script = (app_root / "deploy" / "migrate.sh").read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS dispatch_price_artifacts", migration)
        self.assertIn("raw_zip BYTEA NOT NULL", migration)
        self.assertIn("PUBLIC_DISPATCHIS_", migration)
        self.assertIn("003_dispatch_price_artifacts.sql", migrate_script)
        self.assertIn("SELECT count(*) = 7", migrate_script)
        self.assertIn("'dispatch_price_artifacts'", migrate_script)
        self.assertIn("database_url=$BATTERYWATCH_DATABASE_URL", migrate_script)
        self.assertEqual(migrate_script.count('--dbname="$database_url"'), 4)
        self.assertNotIn("export PGDATABASE=", migrate_script)


if __name__ == "__main__":
    unittest.main()
