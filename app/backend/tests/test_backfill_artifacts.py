"""Behavior tests for immutable historical archive artifact registration."""

from datetime import date, datetime, timezone
import hashlib
import unittest

from batterywatch_api.backfill_artifacts import (
    BackfillArtifactReceipt,
    BackfillArtifactResult,
    PostgreSQLBackfillArtifactRegistrar,
)
from batterywatch_api.backfill_ledger import BackfillClaim


UTC = timezone.utc
REPORT_DATE = date(2026, 8, 28)
SOURCE_URL = (
    "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
    "PUBLIC_DISPATCHIS_20260828.zip"
)


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
        return None

    def close(self) -> None:
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(self, *, fetchone_results=(), fail_on_execute=None, failure=None) -> None:
        self.fetchone_results = list(fetchone_results)
        self.fail_on_execute = fail_on_execute
        self.failure = failure
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


def receipt(raw_archive: bytes = b"official archive bytes") -> BackfillArtifactReceipt:
    return BackfillArtifactReceipt(
        BackfillClaim(
            "run-20260828",
            "dispatch_price",
            REPORT_DATE,
            SOURCE_URL,
            3,
        ),
        datetime(2026, 8, 29, 2, tzinfo=UTC),
        datetime(2026, 8, 29, 1, 30, tzinfo=UTC),
        raw_archive,
    )


class PostgreSQLBackfillArtifactRegistrarTests(unittest.TestCase):
    def test_records_content_link_and_event_in_one_transaction(self) -> None:
        evidence = receipt()
        digest = hashlib.sha256(evidence.raw_archive).hexdigest()
        connection = FakeConnection(
            fetchone_results=(
                (SOURCE_URL, "running", 3),
                (1,),
                (1,),
            )
        )

        result = PostgreSQLBackfillArtifactRegistrar(connection).record(evidence)

        self.assertEqual(
            result,
            BackfillArtifactResult(digest, len(evidence.raw_archive), False),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))
        self.assertEqual(len(connection.executions), 4)

        lock_sql, lock_parameters = connection.executions[0]
        self.assertIn("FROM historical_backfill_items", lock_sql)
        self.assertIn("FOR UPDATE", lock_sql)
        self.assertEqual(
            lock_parameters,
            ("run-20260828", "dispatch_price", REPORT_DATE),
        )

        artifact_sql, artifact_parameters = connection.executions[1]
        self.assertIn("INSERT INTO historical_source_artifacts", artifact_sql)
        self.assertIn("ON CONFLICT DO NOTHING RETURNING 1", artifact_sql)
        self.assertEqual(
            artifact_parameters,
            (
                digest,
                "dispatch_price",
                REPORT_DATE,
                SOURCE_URL,
                "PUBLIC_DISPATCHIS_20260828.zip",
                len(evidence.raw_archive),
                evidence.raw_archive,
            ),
        )

        link_sql, link_parameters = connection.executions[2]
        self.assertIn("INSERT INTO historical_backfill_item_artifacts", link_sql)
        self.assertIn("ON CONFLICT DO NOTHING RETURNING 1", link_sql)
        self.assertEqual(
            link_parameters,
            (
                "run-20260828",
                "dispatch_price",
                REPORT_DATE,
                3,
                digest,
                evidence.downloaded_at,
                evidence.source_last_modified,
            ),
        )

        event_sql, event_parameters = connection.executions[3]
        self.assertIn("INSERT INTO historical_backfill_events", event_sql)
        self.assertEqual(
            event_parameters,
            ("run-20260828", "dispatch_price", REPORT_DATE, "artifact_recorded", 3),
        )


if __name__ == "__main__":
    unittest.main()
