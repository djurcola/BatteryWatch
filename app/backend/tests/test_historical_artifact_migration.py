"""Contract tests for the immutable historical source-artifact schema."""

from pathlib import Path
import unittest


class HistoricalArtifactMigrationTests(unittest.TestCase):
    def test_migration_defines_immutable_artifacts_and_deploys_last(self) -> None:
        app_root = Path(__file__).resolve().parents[2]
        migration = (app_root / "migrations" / "005_historical_source_artifacts.sql").read_text(
            encoding="utf-8"
        )
        migrate_script = (app_root / "deploy" / "migrate.sh").read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS historical_source_artifacts", migration)
        self.assertIn(
            "artifact_sha256 TEXT PRIMARY KEY CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$')",
            migration,
        )
        self.assertIn("feed", migration)
        self.assertIn("dispatch_price", migration)
        self.assertIn("dispatch_scada", migration)
        self.assertIn("report_date", migration)
        self.assertIn("source_url", migration)
        self.assertIn("filename", migration)
        self.assertIn("byte_count", migration)
        self.assertIn("raw_bytes BYTEA NOT NULL", migration)
        self.assertIn("stored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP", migration)
        self.assertIn("UNIQUE (artifact_sha256, feed, report_date)", migration)
        self.assertIn("byte_count = octet_length(raw_bytes)", migration)
        self.assertIn("DispatchIS_Reports/", migration)
        self.assertIn("Dispatch_SCADA/", migration)
        self.assertIn("to_char(report_date, 'YYYYMMDD')", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS historical_backfill_item_artifacts", migration)
        self.assertIn("PRIMARY KEY (run_id, feed, report_date, attempt_number)", migration)
        self.assertIn("attempt_number > 0", migration)
        self.assertIn("REFERENCES historical_backfill_items", migration)
        self.assertIn("REFERENCES historical_source_artifacts", migration)
        self.assertIn("ON DELETE RESTRICT", migration)
        self.assertIn("downloaded_at TIMESTAMPTZ NOT NULL", migration)
        self.assertIn("source_last_modified TIMESTAMPTZ", migration)
        self.assertIn("recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP", migration)
        self.assertIn("source_last_modified IS NULL OR source_last_modified <= downloaded_at", migration)
        self.assertIn("historical_source_artifacts_feed_report_date_idx", migration)
        self.assertIn("historical_backfill_item_artifacts_artifact_sha256_idx", migration)

        self.assertIn("004_historical_backfill_ledger.sql", migrate_script)
        self.assertIn("005_historical_source_artifacts.sql", migrate_script)
        self.assertIn("SELECT count(*) = 13", migrate_script)
        for table in ("historical_source_artifacts", "historical_backfill_item_artifacts"):
            self.assertIn(f"'{table}'", migrate_script)
        self.assertEqual(migrate_script.count('--dbname="$database_url"'), 9)

        upper = migration.upper()
        for forbidden in ("DROP ", "ALTER ", "TRUNCATE "):
            self.assertNotIn(forbidden, upper)
        self.assertNotRegex(upper, r"\bDELETE\s+FROM\b")

    def test_expands_event_constraint_for_artifact_recorded_atomically(self) -> None:
        app_root = Path(__file__).resolve().parents[2]
        migration_path = app_root / "migrations" / "006_artifact_recorded_event.sql"
        self.assertTrue(migration_path.exists(), "006 event migration is missing")

        migration = migration_path.read_text(encoding="utf-8")
        migrate_script = (app_root / "deploy" / "migrate.sh").read_text(
            encoding="utf-8"
        )
        upper = migration.upper()

        self.assertIn("BEGIN;", upper)
        self.assertIn("COMMIT;", upper)
        self.assertIn("ALTER TABLE historical_backfill_events", migration)
        self.assertIn(
            "DROP CONSTRAINT IF EXISTS historical_backfill_events_event_type_check",
            migration,
        )
        self.assertIn(
            "ADD CONSTRAINT historical_backfill_events_event_type_check",
            migration,
        )
        for event_type in (
            "planned",
            "recovered",
            "claimed",
            "artifact_recorded",
            "completed",
            "failed",
        ):
            self.assertIn(f"'{event_type}'", migration)
        self.assertIn("006_artifact_recorded_event.sql", migrate_script)
        self.assertEqual(migrate_script.count('--dbname="$database_url"'), 9)
        self.assertNotIn("TRUNCATE ", upper)
        self.assertNotRegex(upper, r"\bDELETE\s+FROM\b")

    def test_adds_runtime_ledger_error_and_event_detail_columns(self) -> None:
        app_root = Path(__file__).resolve().parents[2]
        migration_path = app_root / "migrations" / "007_backfill_runtime_details.sql"
        self.assertTrue(migration_path.exists(), "007 runtime-details migration is missing")

        migration = migration_path.read_text(encoding="utf-8")
        migrate_script = (app_root / "deploy" / "migrate.sh").read_text(
            encoding="utf-8"
        )
        upper = migration.upper()

        self.assertIn("BEGIN;", upper)
        self.assertIn("COMMIT;", upper)
        self.assertIn("ALTER TABLE historical_backfill_items", migration)
        self.assertIn("ADD COLUMN IF NOT EXISTS last_error TEXT", migration)
        self.assertIn("historical_backfill_items_last_error_check", migration)
        self.assertIn("char_length(last_error) BETWEEN 1 AND 2048", migration)
        self.assertIn("ALTER TABLE historical_backfill_events", migration)
        self.assertIn("ADD COLUMN IF NOT EXISTS details JSONB", migration)
        self.assertIn("DEFAULT '{}'::jsonb", migration)
        self.assertIn("historical_backfill_events_details_check", migration)
        self.assertIn("jsonb_typeof(details) = 'object'", migration)
        self.assertIn("007_backfill_runtime_details.sql", migrate_script)
        self.assertEqual(migrate_script.count('--dbname="$database_url"'), 9)
        self.assertNotIn("TRUNCATE ", upper)
        self.assertNotRegex(upper, r"\bDELETE\s+FROM\b")

    def test_adds_authoritative_individual_soc_schema_after_runtime_details(self) -> None:
        app_root = Path(__file__).resolve().parents[2]
        migration_path = app_root / "migrations" / "008_authoritative_soc.sql"
        self.assertTrue(migration_path.exists(), "008 authoritative-SOC migration is missing")

        migration = migration_path.read_text(encoding="utf-8")
        migrate_script = (app_root / "deploy" / "migrate.sh").read_text(encoding="utf-8")
        upper = migration.upper()

        self.assertIn("BEGIN;", upper)
        self.assertIn("COMMIT;", upper)
        self.assertIn("CREATE TABLE IF NOT EXISTS raw_nextday_soc_observations", migration)
        for column in (
            "artifact_sha256",
            "generator_id",
            "interval_start",
            "soc_mwh",
            "intervention",
            "run_number",
            "dispatch_interval",
            "last_changed",
            "report_timestamp",
            "downloaded_at",
            "ingestion_version",
            "correction_version",
        ):
            self.assertIn(column, migration)
        self.assertIn("REFERENCES historical_source_artifacts", migration)
        self.assertIn("REFERENCES generators", migration)
        self.assertIn("ON DELETE RESTRICT", migration)
        self.assertIn("raw_nextday_soc_observations_artifact_time_idx", migration)
        self.assertIn("raw_nextday_soc_observations_generator_time_idx", migration)

        self.assertIn("ALTER TABLE generator_soc_5m", migration)
        for column in (
            "soc_mwh",
            "capacity_mwh",
            "capacity_effective_from",
            "capacity_effective_to",
            "capacity_source_id",
            "capacity_source_timestamp",
            "report_timestamp",
            "downloaded_at",
            "intervention",
            "run_number",
            "dispatch_interval",
            "source_artifact_sha256",
        ):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", migration)
        self.assertIn("generator_soc_5m_authoritative_soc_check", migration)
        self.assertIn("generator_soc_5m_capacity_provenance_check", migration)
        self.assertIn("generator_soc_5m_authoritative_metadata_check", migration)
        self.assertGreaterEqual(
            migration.count("num_nonnulls("),
            4,
            "all-or-none SOC provenance checks must not pass through SQL NULL",
        )
        self.assertIn("'nextday_soc'", migration)
        self.assertIn("PUBLIC_NEXT_DAY_DISPATCH_", migration)
        self.assertIn("Next_Day_Dispatch/", migration)
        for column in (
            "parent_artifact_sha256",
            "artifact_published_at",
            "artifact_downloaded_at",
            "publication_id",
        ):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", migration)
        self.assertIn("historical_source_artifacts_parent_fk", migration)
        self.assertIn("REFERENCES historical_source_artifacts (artifact_sha256)", migration)
        self.assertIn("historical_source_artifacts_nested_metadata_ck", migration)
        self.assertRegex(
            migration,
            r"num_nonnulls\(\s*parent_artifact_sha256,\s*"
            r"artifact_published_at,\s*artifact_downloaded_at,\s*"
            r"publication_id\s*\)",
        )
        self.assertIn("artifact_published_at <= artifact_downloaded_at", migration)
        self.assertIn("source_url =", migration)
        self.assertIn("|| '#' || filename", migration)
        self.assertIn("historical_source_artifacts_parent_idx", migration)
        for constraint in (
            "historical_backfill_items_feed_check",
            "historical_backfill_events_feed_check",
            "historical_source_artifacts_feed_check",
            "historical_source_artifacts_archive_identity_ck",
            "historical_backfill_item_artifacts_feed_check",
        ):
            self.assertIn(f"DROP CONSTRAINT IF EXISTS {constraint}", migration)
            self.assertIn(f"ADD CONSTRAINT {constraint}", migration)

        self.assertIn("008_authoritative_soc.sql", migrate_script)
        self.assertLess(
            migrate_script.index("007_backfill_runtime_details.sql"),
            migrate_script.index("008_authoritative_soc.sql"),
        )
        self.assertIn("SELECT count(*) = 13", migrate_script)
        self.assertIn("'raw_nextday_soc_observations'", migrate_script)
        self.assertEqual(migrate_script.count('--dbname="$database_url"'), 9)
        for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE ", "DELETE FROM"):
            self.assertNotIn(forbidden, upper)


if __name__ == "__main__":
    unittest.main()
