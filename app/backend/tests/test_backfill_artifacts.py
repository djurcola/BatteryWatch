"""Behavior tests for immutable historical archive artifact registration."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import unittest
from unittest.mock import patch

from batterywatch_api.backfill_artifacts import (
    BackfillArtifactConflictError,
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
    def __init__(
        self,
        *,
        fetchone_results=(),
        fail_on_execute=None,
        failure=None,
        rollback_failure=None,
    ) -> None:
        self.fetchone_results = list(fetchone_results)
        self.fail_on_execute = fail_on_execute
        self.failure = failure
        self.rollback_failure = rollback_failure
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
        if self.rollback_failure is not None:
            raise self.rollback_failure


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

    def test_exact_replay_compares_content_and_link_without_duplicate_event(self) -> None:
        evidence = receipt()
        digest = hashlib.sha256(evidence.raw_archive).hexdigest()
        connection = FakeConnection(
            fetchone_results=(
                (SOURCE_URL, "running", 3),
                None,
                (
                    "dispatch_price",
                    REPORT_DATE,
                    SOURCE_URL,
                    "PUBLIC_DISPATCHIS_20260828.zip",
                    len(evidence.raw_archive),
                    evidence.raw_archive,
                ),
                None,
                (digest, evidence.downloaded_at, evidence.source_last_modified),
            )
        )

        result = PostgreSQLBackfillArtifactRegistrar(connection).record(evidence)

        self.assertEqual(
            result,
            BackfillArtifactResult(digest, len(evidence.raw_archive), True),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual(len(connection.executions), 5)
        self.assertIn(
            "SELECT feed, report_date, source_url, filename, byte_count, raw_archive",
            connection.executions[2][0],
        )
        self.assertEqual(connection.executions[2][1], (digest,))
        self.assertIn(
            "SELECT artifact_sha256, downloaded_at, source_last_modified",
            connection.executions[4][0],
        )
        self.assertEqual(
            connection.executions[4][1],
            ("run-20260828", "dispatch_price", REPORT_DATE, 3),
        )
        self.assertNotIn(
            "INSERT INTO historical_backfill_events",
            "\n".join(statement for statement, _ in connection.executions),
        )

    def test_conflicting_content_rolls_back_and_fails_closed(self) -> None:
        evidence = receipt()
        connection = FakeConnection(
            fetchone_results=(
                (SOURCE_URL, "running", 3),
                None,
                (
                    "dispatch_price",
                    REPORT_DATE,
                    SOURCE_URL,
                    "PUBLIC_DISPATCHIS_20260828.zip",
                    len(evidence.raw_archive),
                    b"different bytes",
                ),
            )
        )

        with self.assertRaisesRegex(
            BackfillArtifactConflictError,
            "source artifact",
        ):
            PostgreSQLBackfillArtifactRegistrar(connection).record(evidence)

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))

    def test_conflicting_attempt_link_rolls_back_and_fails_closed(self) -> None:
        evidence = receipt()
        digest = hashlib.sha256(evidence.raw_archive).hexdigest()
        connection = FakeConnection(
            fetchone_results=(
                (SOURCE_URL, "running", 3),
                None,
                (
                    "dispatch_price",
                    REPORT_DATE,
                    SOURCE_URL,
                    "PUBLIC_DISPATCHIS_20260828.zip",
                    len(evidence.raw_archive),
                    evidence.raw_archive,
                ),
                None,
                (
                    digest,
                    evidence.downloaded_at + timedelta(minutes=1),
                    evidence.source_last_modified,
                ),
            )
        )

        with self.assertRaisesRegex(
            BackfillArtifactConflictError,
            "artifact link",
        ):
            PostgreSQLBackfillArtifactRegistrar(connection).record(evidence)

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))

    def test_stale_claim_rolls_back_before_artifact_sql(self) -> None:
        connection = FakeConnection(
            fetchone_results=((SOURCE_URL, "pending", 2),)
        )

        with self.assertRaisesRegex(
            BackfillArtifactConflictError,
            "current backfill claim",
        ):
            PostgreSQLBackfillArtifactRegistrar(connection).record(receipt())

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual(len(connection.executions), 1)

    def test_invalid_receipts_fail_before_opening_a_cursor(self) -> None:
        valid = receipt()
        invalid_receipts = (
            object(),
            replace(valid, claim=replace(valid.claim, run_id="bad run id")),
            replace(valid, claim=replace(valid.claim, feed="regional_soc")),
            replace(valid, claim=replace(valid.claim, report_date=datetime(2026, 8, 28))),
            replace(valid, claim=replace(valid.claim, source_url=SOURCE_URL + ".wrong")),
            replace(valid, claim=replace(valid.claim, attempt_number=0)),
            replace(valid, claim=replace(valid.claim, attempt_number=True)),
            replace(valid, downloaded_at=datetime(2026, 8, 29, 2)),
            replace(valid, source_last_modified=datetime(2026, 8, 29, 1, 30)),
            replace(
                valid,
                source_last_modified=valid.downloaded_at + timedelta(minutes=1),
            ),
            replace(valid, raw_archive=b""),
            replace(valid, raw_archive=bytearray(b"mutable")),
        )

        for invalid in invalid_receipts:
            with self.subTest(invalid=invalid):
                connection = FakeConnection()
                with self.assertRaises((TypeError, ValueError)):
                    PostgreSQLBackfillArtifactRegistrar(connection).record(
                        invalid  # type: ignore[arg-type]
                    )
                self.assertEqual(connection.cursor_calls, 0)

        connection = FakeConnection()
        with patch(
            "batterywatch_api.backfill_artifacts.MAX_ARCHIVE_BYTES",
            len(valid.raw_archive) - 1,
        ):
            with self.assertRaisesRegex(ValueError, "raw_archive"):
                PostgreSQLBackfillArtifactRegistrar(connection).record(valid)
        self.assertEqual(connection.cursor_calls, 0)

    def test_database_failure_preserves_original_when_rollback_also_fails(self) -> None:
        database_failure = RuntimeError("database write failed")
        connection = FakeConnection(
            fetchone_results=((SOURCE_URL, "running", 3),),
            fail_on_execute=2,
            failure=database_failure,
            rollback_failure=RuntimeError("rollback failed"),
        )

        with self.assertRaises(RuntimeError) as raised:
            PostgreSQLBackfillArtifactRegistrar(connection).record(receipt())

        self.assertIs(raised.exception, database_failure)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))


if __name__ == "__main__":
    unittest.main()
