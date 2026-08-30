"""Static contract tests for the raw Dispatch SCADA migration."""

from pathlib import Path
import unittest


class DispatchScadaIngestionSchemaTests(unittest.TestCase):
    def test_migration_defines_raw_dispatch_scada_schema(self) -> None:
        migration = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "002_dispatch_scada_raw_ingestion.sql"
        ).read_text(encoding="utf-8")
        required_fragments = {
            "artifact_table": "CREATE TABLE IF NOT EXISTS dispatch_scada_artifacts",
            "artifact_id": "source_artifact_id TEXT PRIMARY KEY",
            "artifact_id_non_empty_numeric": (
                "source_artifact_id <> ''"
                " AND source_artifact_id ~ '^[0-9]+$'"
            ),
            "canonical_https_url": (
                "source_url TEXT NOT NULL"
                " CHECK (source_url <> '' AND source_url ~ '^https://')"
            ),
            "canonical_nemweb_url": (
                "source_url = 'https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/'"
                " || zip_filename"
            ),
            "zip_filename": "zip_filename TEXT NOT NULL CHECK (btrim(zip_filename) <> '')",
            "canonical_zip_filename": (
                "zip_filename ~ ('^PUBLIC_DISPATCHSCADA_[0-9]{12}_'"
                " || source_artifact_id || '[.]zip$')"
            ),
            "csv_member_name": (
                "csv_member_name TEXT NOT NULL"
                " CHECK (btrim(csv_member_name) <> '')"
            ),
            "canonical_csv_member_name": (
                "csv_member_name = left(zip_filename, -4) || '.CSV'"
            ),
            "report_timestamp": "report_timestamp TIMESTAMPTZ NOT NULL",
            "sha256": (
                "zip_sha256 TEXT NOT NULL"
                " CHECK (zip_sha256 ~ '^[0-9a-f]{64}$')"
            ),
            "raw_zip": "raw_zip BYTEA NOT NULL CHECK (octet_length(raw_zip) > 0)",
            "artifact_stored_at": (
                "stored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ),
            "observation_table": (
                "CREATE TABLE IF NOT EXISTS raw_dispatch_scada_observations"
            ),
            "observation_artifact_id": "source_artifact_id TEXT NOT NULL",
            "artifact_foreign_key": (
                "REFERENCES dispatch_scada_artifacts (source_artifact_id)"
                " ON DELETE RESTRICT"
            ),
            "duid": "duid TEXT NOT NULL CHECK (btrim(duid) <> '')",
            "interval": "interval_start TIMESTAMPTZ NOT NULL",
            "interval_minute_alignment": (
                "date_trunc('minute', interval_start) = interval_start"
            ),
            "interval_five_minute_alignment": (
                "EXTRACT(MINUTE FROM interval_start)::INTEGER % 5 = 0"
            ),
            "power": "power_mw DOUBLE PRECISION NOT NULL",
            "finite_power": (
                "power_mw::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')"
            ),
            "source_timestamp": "source_timestamp TIMESTAMPTZ NOT NULL",
            "ingestion_version": (
                "ingestion_version BIGINT NOT NULL DEFAULT 0"
                " CHECK (ingestion_version >= 0)"
            ),
            "correction_version": (
                "correction_version BIGINT NOT NULL DEFAULT 0"
                " CHECK (correction_version >= 0)"
            ),
            "observation_stored_at": (
                "stored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ),
            "artifact_specific_key": (
                "PRIMARY KEY (source_artifact_id, duid, interval_start)"
            ),
            "artifact_time_index": (
                "CREATE INDEX IF NOT EXISTS dispatch_scada_artifacts_report_timestamp_idx"
                " ON dispatch_scada_artifacts (report_timestamp DESC)"
            ),
            "observation_time_index": (
                "CREATE INDEX IF NOT EXISTS raw_dispatch_scada_observations_interval_idx"
                " ON raw_dispatch_scada_observations (interval_start DESC)"
            ),
        }
        self.assertEqual(
            {name: fragment in migration for name, fragment in required_fragments.items()},
            {name: True for name in required_fragments},
        )
        self.assertNotIn("generators", migration.lower())
        self.assertNotIn("generator_power_5m", migration.lower())
        self.assertNotIn("hypertable", migration.lower())


if __name__ == "__main__":
    unittest.main()
