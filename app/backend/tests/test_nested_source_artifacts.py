"""Tests for immutable nested Next Day source-artifact registration."""

from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import unittest

from batterywatch_api.nested_source_artifacts import (
    NestedSourceArtifactConflictError,
    NestedSourceArtifactReceipt,
    NestedSourceArtifactResult,
    PostgreSQLNestedSourceArtifactRegistrar,
)

UTC = timezone.utc
PARENT_SHA = "a" * 64
OUTER_URL = (
    "https://www.nemweb.com.au/REPORTS/ARCHIVE/Next_Day_Dispatch/"
    "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip"
)
DAILY_FILENAME = "PUBLIC_NEXT_DAY_DISPATCH_20250701_0000000470129643.zip"


class FakeCursor:
    def __init__(self, connection) -> None:
        self.connection = connection

    def execute(self, statement, parameters) -> None:
        self.connection.executions.append((statement, tuple(parameters)))

    def fetchone(self):
        if self.connection.fetchone_results:
            return self.connection.fetchone_results.pop(0)
        return None

    def close(self) -> None:
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(self, fetchone_results=()) -> None:
        self.fetchone_results = list(fetchone_results)
        self.executions = []
        self.cursor_calls = self.closed_cursors = 0
        self.commits = self.rollbacks = 0

    def cursor(self):
        self.cursor_calls += 1
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def receipt(raw_bytes: bytes = b"official nested daily zip") -> NestedSourceArtifactReceipt:
    return NestedSourceArtifactReceipt(
        PARENT_SHA,
        date(2025, 7, 1),
        OUTER_URL,
        DAILY_FILENAME,
        "0000000470129643",
        datetime(2025, 7, 1, 18, 11, 26, tzinfo=UTC),
        datetime(2026, 8, 30, tzinfo=UTC),
        raw_bytes,
    )


class PostgreSQLNestedSourceArtifactRegistrarTests(unittest.TestCase):
    def test_registers_daily_artifact_under_canonical_monthly_parent(self) -> None:
        evidence = receipt()
        digest = hashlib.sha256(evidence.raw_bytes).hexdigest()
        connection = FakeConnection(
            (("nextday_soc", date(2025, 7, 1), OUTER_URL, None), (1,))
        )

        result = PostgreSQLNestedSourceArtifactRegistrar(connection).record(evidence)

        self.assertEqual(
            result,
            NestedSourceArtifactResult(
                digest, len(evidence.raw_bytes), evidence.downloaded_at, False
            ),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))
        self.assertEqual(len(connection.executions), 2)
        self.assertIn("FOR SHARE", connection.executions[0][0])
        self.assertEqual(connection.executions[0][1], (PARENT_SHA,))
        insert_sql, parameters = connection.executions[1]
        self.assertIn("INSERT INTO historical_source_artifacts", insert_sql)
        self.assertIn("parent_artifact_sha256", insert_sql)
        self.assertIn("artifact_published_at", insert_sql)
        self.assertIn("artifact_downloaded_at", insert_sql)
        self.assertEqual(
            parameters,
            (
                digest,
                "nextday_soc",
                evidence.report_date,
                f"{OUTER_URL}#{DAILY_FILENAME}",
                DAILY_FILENAME,
                len(evidence.raw_bytes),
                evidence.raw_bytes,
                PARENT_SHA,
                evidence.artifact_published_at,
                evidence.downloaded_at,
                evidence.publication_id,
            ),
        )

    def test_exact_replay_verifies_stored_daily_artifact(self) -> None:
        evidence = receipt()
        digest = hashlib.sha256(evidence.raw_bytes).hexdigest()
        source_url = f"{OUTER_URL}#{DAILY_FILENAME}"
        connection = FakeConnection(
            (
                ("nextday_soc", date(2025, 7, 1), OUTER_URL, None),
                None,
                (
                    "nextday_soc",
                    evidence.report_date,
                    source_url,
                    evidence.filename,
                    len(evidence.raw_bytes),
                    evidence.raw_bytes,
                    evidence.parent_artifact_sha256,
                    evidence.artifact_published_at,
                    evidence.downloaded_at,
                    evidence.publication_id,
                ),
            )
        )

        result = PostgreSQLNestedSourceArtifactRegistrar(connection).record(evidence)

        self.assertEqual(
            result,
            NestedSourceArtifactResult(
                digest, len(evidence.raw_bytes), evidence.downloaded_at, True
            ),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual(len(connection.executions), 3)
        self.assertIn("SELECT feed, report_date", connection.executions[2][0])
        self.assertEqual(connection.executions[2][1], (digest,))

    def test_later_download_receipt_replays_without_replacing_first_seen_time(self) -> None:
        stored_evidence = receipt()
        replay_evidence = replace(
            stored_evidence,
            downloaded_at=stored_evidence.downloaded_at + timedelta(days=1),
        )
        digest = hashlib.sha256(replay_evidence.raw_bytes).hexdigest()
        source_url = f"{OUTER_URL}#{DAILY_FILENAME}"
        connection = FakeConnection(
            (
                ("nextday_soc", date(2025, 7, 1), OUTER_URL, None),
                None,
                (
                    "nextday_soc",
                    stored_evidence.report_date,
                    source_url,
                    stored_evidence.filename,
                    len(stored_evidence.raw_bytes),
                    stored_evidence.raw_bytes,
                    stored_evidence.parent_artifact_sha256,
                    stored_evidence.artifact_published_at,
                    stored_evidence.downloaded_at,
                    stored_evidence.publication_id,
                ),
            )
        )

        result = PostgreSQLNestedSourceArtifactRegistrar(connection).record(
            replay_evidence
        )

        self.assertEqual(
            result,
            NestedSourceArtifactResult(
                digest,
                len(replay_evidence.raw_bytes),
                stored_evidence.downloaded_at,
                True,
            ),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))

    def test_same_sha_with_changed_publication_metadata_fails_closed(self) -> None:
        evidence = receipt()
        source_url = f"{OUTER_URL}#{DAILY_FILENAME}"
        connection = FakeConnection(
            (
                ("nextday_soc", date(2025, 7, 1), OUTER_URL, None),
                None,
                (
                    "nextday_soc",
                    evidence.report_date,
                    source_url,
                    evidence.filename,
                    len(evidence.raw_bytes),
                    evidence.raw_bytes,
                    evidence.parent_artifact_sha256,
                    evidence.artifact_published_at + timedelta(seconds=2),
                    evidence.downloaded_at,
                    evidence.publication_id,
                ),
            )
        )

        with self.assertRaisesRegex(
            NestedSourceArtifactConflictError,
            "conflicting nested",
        ):
            PostgreSQLNestedSourceArtifactRegistrar(connection).record(evidence)

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))

    def test_wrong_parent_artifact_fails_closed(self) -> None:
        connection = FakeConnection(
            (("dispatch_price", date(2025, 7, 1), OUTER_URL, None),)
        )

        with self.assertRaisesRegex(
            NestedSourceArtifactConflictError,
            "parent conflicts",
        ):
            PostgreSQLNestedSourceArtifactRegistrar(connection).record(receipt())

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual(len(connection.executions), 1)

    def test_invalid_publication_identity_is_rejected_before_db_access(self) -> None:
        invalid = replace(receipt(), publication_id="guessed")
        connection = FakeConnection()

        with self.assertRaisesRegex(ValueError, "publication"):
            PostgreSQLNestedSourceArtifactRegistrar(connection).record(invalid)

        self.assertEqual(connection.cursor_calls, 0)


if __name__ == "__main__":
    unittest.main()
