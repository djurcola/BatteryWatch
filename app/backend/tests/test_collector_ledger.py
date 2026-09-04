"""Tests for the durable bounded CURRENT artifact ledger."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from batterywatch_api.collector_ledger import (
    CurrentArtifactClaim,
    CurrentArtifactCompletion,
    CurrentArtifactLedgerConflictError,
    CurrentArtifactLedgerReconciliation,
    CurrentArtifactMiss,
    CurrentArtifactReference,
    PostgreSQLCurrentArtifactLedger,
    bounded_error_summary,
)


UTC = timezone.utc


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters):
        self.connection.executions.append((statement, tuple(parameters)))

    def fetchone(self):
        if self.connection.fetchone_results:
            return self.connection.fetchone_results.pop(0)
        return None

    def fetchall(self):
        if self.connection.fetchall_results:
            return self.connection.fetchall_results.pop(0)
        return []

    def close(self):
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(self, *, fetchone_results=(), fetchall_results=()):
        self.fetchone_results = list(fetchone_results)
        self.fetchall_results = list(fetchall_results)
        self.executions = []
        self.closed_cursors = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def reference(source_id: str, minute: int) -> CurrentArtifactReference:
    filename = f"PUBLIC_DISPATCHSCADA_2026082912{minute:02d}_{source_id}.zip"
    return CurrentArtifactReference(
        "dispatch_scada",
        source_id,
        "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
        filename,
        datetime(2026, 8, 29, 2, minute, tzinfo=UTC),
    )


class CollectorLedgerMigrationTests(unittest.TestCase):
    def test_additive_ledger_is_deployed_after_archive_support_and_fcass(self):
        root = Path(__file__).resolve().parents[2]
        migration = (root / "migrations" / "009_current_artifact_ledger.sql").read_text()
        script = (root / "deploy" / "migrate.sh").read_text()

        for fragment in (
            "CREATE TABLE IF NOT EXISTS current_artifact_ledger",
            "PRIMARY KEY (feed, source_artifact_id)",
            "status IN ('pending', 'completed', 'missed')",
            "attempt_count BIGINT NOT NULL",
            "last_attempted_at TIMESTAMPTZ",
            "current_artifact_ledger_selection_idx",
            "CURRENT/Dispatch_SCADA/",
            "CURRENT/DispatchIS_Reports/",
        ):
            self.assertIn(fragment, migration)
        self.assertNotIn("ARCHIVE", migration)
        self.assertIn("009_allow_archive_urls.sql", script)
        self.assertIn("009_current_artifact_ledger.sql", script)
        self.assertLess(
            script.index("009_allow_archive_urls.sql"),
            script.index("009_current_artifact_ledger.sql"),
        )
        for name in (
            "010_nextday_fcas.sql",
            "011_fcas_validator_compatibility.sql",
            "012_current_nextday_artifacts.sql",
        ):
            self.assertIn(name, script)
        self.assertIn("SELECT count(*) = 16", script)
        self.assertIn("'current_artifact_ledger'", script)

    def test_deployed_service_contract_keeps_ledger_runtime_and_180_second_cadence(self):
        root = Path(__file__).resolve().parents[2]
        collector_service = (
            root / "backend" / "batterywatch_api" / "collector_service.py"
        ).read_text(encoding="utf-8")
        unit = (root / "deploy" / "batterywatch-collector.service").read_text(
            encoding="utf-8"
        )

        self.assertTrue(
            (root / "backend" / "batterywatch_api" / "collector_ledger.py").is_file()
        )
        self.assertIn("from .collector_ledger import (", collector_service)
        self.assertIn("return run_current_collection_cycle(", collector_service)
        self.assertIn("--interval-seconds 180", unit)
        self.assertNotIn("--interval-seconds 300", unit)


class PostgreSQLCurrentArtifactLedgerTests(unittest.TestCase):
    def test_reconcile_inserts_new_identity_and_allows_same_id_on_other_feed(self):
        item = reference("42", 0)
        price = CurrentArtifactReference(
            "dispatch_price",
            "42",
            "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_202608291200_42.zip",
            "PUBLIC_DISPATCHIS_202608291200_42.zip",
            datetime(2026, 8, 29, 2, 0, tzinfo=UTC),
        )
        connection = FakeConnection(fetchone_results=((1,), (1,)))

        result = PostgreSQLCurrentArtifactLedger(connection).reconcile((item, price))

        self.assertEqual(result, CurrentArtifactLedgerReconciliation(2, 2))
        self.assertEqual(connection.commits, 1)
        self.assertEqual(len(connection.executions), 2)
        self.assertEqual(
            connection.executions[0][1][:2], ("dispatch_scada", "42")
        )
        self.assertEqual(
            connection.executions[1][1][:2], ("dispatch_price", "42")
        )

    def test_reconcile_conflicting_existing_identity_fails_closed(self):
        item = reference("42", 0)
        stored = (
            item.source_url,
            item.zip_filename,
            datetime(2026, 8, 29, 2, 5, tzinfo=UTC),
            "completed",
            1,
        )
        connection = FakeConnection(fetchone_results=(None, stored))

        with self.assertRaises(CurrentArtifactLedgerConflictError):
            PostgreSQLCurrentArtifactLedger(connection).reconcile((item,))
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))

    def test_new_rows_are_immediately_claimable_and_claim_is_optimistic(self):
        item = reference("1", 0)
        connection = FakeConnection(
            fetchone_results=((1,), (1,)),
            fetchall_results=(
                [(
                    item.feed,
                    item.source_artifact_id,
                    item.source_url,
                    item.zip_filename,
                    item.report_timestamp,
                    "pending",
                    0,
                )],
            ),
        )
        ledger = PostgreSQLCurrentArtifactLedger(connection)

        self.assertEqual(ledger.reconcile((item,)), CurrentArtifactLedgerReconciliation(1, 1))
        self.assertEqual(
            ledger.claim_pending("dispatch_scada", limit=1),
            (CurrentArtifactClaim(item, 1),),
        )
        self.assertIn("last_attempted_at IS NULL", connection.executions[1][0])
        self.assertIn("status IN ('pending', 'missed')", connection.executions[1][0])
        self.assertIn("FOR UPDATE OF ledger SKIP LOCKED", connection.executions[1][0])
        self.assertIn("attempt_count = %s", connection.executions[2][0])

    def test_retry_window_is_durable_and_has_no_attempt_cap(self):
        connection = FakeConnection(fetchall_results=([],))

        PostgreSQLCurrentArtifactLedger(connection).claim_pending(
            "dispatch_scada", limit=1
        )
        statement, parameters = connection.executions[0]
        self.assertEqual(parameters, ("dispatch_scada", 0, 1))
        self.assertIn(
            "last_attempted_at <= CURRENT_TIMESTAMP - INTERVAL '15 minutes'",
            statement,
        )
        self.assertNotIn("attempt_count <", statement)
        self.assertNotIn("attempt_count <=", statement)

    def test_limit_one_uses_newest_lane_and_union_deduplicates_fair_lanes(self):
        older = reference("1", 0)
        newer = reference("2", 5)

        class SelectionCursor(FakeCursor):
            def fetchall(self):
                rows = super().fetchall()
                _, parameters = self.connection.executions[0]
                oldest, newest = parameters[1:]
                ordered = sorted(rows, key=lambda row: row[4])
                selected = ordered[:oldest]
                if newest:
                    selected.extend(ordered[-newest:])
                return selected

        class SelectionConnection(FakeConnection):
            def cursor(self):
                return SelectionCursor(self)

        connection = SelectionConnection(
            fetchall_results=(
                [
                    (
                        item.feed,
                        item.source_artifact_id,
                        item.source_url,
                        item.zip_filename,
                        item.report_timestamp,
                        "pending",
                        0,
                    )
                    for item in (older, newer)
                ],
            ),
            fetchone_results=((1,),),
        )
        claims = PostgreSQLCurrentArtifactLedger(connection).claim_pending(
            "dispatch_scada", limit=1
        )
        self.assertEqual(claims, (CurrentArtifactClaim(newer, 1),))
        self.assertIn("UNION", connection.executions[0][0])
        self.assertIn("ORDER BY report_timestamp ASC", connection.executions[0][0])
        self.assertIn("ORDER BY report_timestamp DESC", connection.executions[0][0])

    def test_claim_limit_boundaries_reserve_oldest_and_newest_lanes(self):
        connection = FakeConnection(fetchall_results=([],))
        ledger = PostgreSQLCurrentArtifactLedger(connection)

        self.assertEqual(ledger.claim_pending("dispatch_scada", limit=12), ())
        statement, parameters = connection.executions[0]
        self.assertEqual(parameters, ("dispatch_scada", 6, 6))
        self.assertEqual(statement.count("%s"), len(parameters))

        for invalid_limit in (0, 13, True):
            with self.subTest(limit=invalid_limit):
                with self.assertRaises(ValueError):
                    ledger.claim_pending("dispatch_scada", limit=invalid_limit)
        self.assertEqual(len(connection.executions), 1)

    def test_complete_and_miss_guard_status_and_attempt(self):
        item = reference("1", 0)
        claim = CurrentArtifactClaim(item, 2)
        connection = FakeConnection(
            fetchone_results=(
                (item.source_url, item.zip_filename, item.report_timestamp, "missed", 2),
                (1,),
            )
        )
        completed = PostgreSQLCurrentArtifactLedger(connection).complete(claim)
        self.assertEqual(completed, CurrentArtifactCompletion(False))
        self.assertIn("status IN ('pending', 'missed')", connection.executions[1][0])
        self.assertIn("attempt_count = %s", connection.executions[1][0])

        miss_connection = FakeConnection(
            fetchone_results=(
                (item.source_url, item.zip_filename, item.report_timestamp, "pending", 2),
                (1,),
            )
        )
        missed = PostgreSQLCurrentArtifactLedger(miss_connection).miss(
            claim, error_summary="temporary parser failure"
        )
        self.assertEqual(missed, CurrentArtifactMiss("temporary parser failure"))
        self.assertIn("status = 'missed'", miss_connection.executions[1][0])
        self.assertIn("attempt_count = %s", miss_connection.executions[1][0])

    def test_error_summary_is_bounded_and_redacts_url(self):
        summary = bounded_error_summary(
            "failure https://secret.invalid/?token=private " + "x" * 1000
        )
        self.assertLessEqual(len(summary), 512)
        self.assertNotIn("secret.invalid", summary)
        self.assertNotIn("token=private", summary)
        self.assertEqual(summary, summary.strip())


if __name__ == "__main__":
    unittest.main()
