"""Tests for the bounded CURRENT artifact ledger."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from batterywatch_api.nemweb_dispatch_scada import DispatchScadaArtifactRef
from batterywatch_api.collector_ledger import (
    CurrentArtifactClaim,
    CurrentArtifactCompletion,
    CurrentArtifactLedgerReconciliation,
    CurrentArtifactMiss,
    CurrentArtifactReference,
    PostgreSQLCurrentArtifactLedger,
    bounded_error_summary,
)


class FakeCursor:
    def __init__(self, connection) -> None:
        self.connection = connection

    def execute(self, statement, parameters) -> None:
        self.connection.executions.append((statement, tuple(parameters)))

    def fetchone(self):
        if self.connection.fetchone_results:
            return self.connection.fetchone_results.pop(0)
        return None

    def fetchall(self):
        if self.connection.fetchall_results:
            return self.connection.fetchall_results.pop(0)
        return []

    def close(self) -> None:
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(self, *, fetchone_results=(), fetchall_results=()) -> None:
        self.fetchone_results = list(fetchone_results)
        self.fetchall_results = list(fetchall_results)
        self.executions = []
        self.closed_cursors = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class CurrentArtifactLedgerMigrationTests(unittest.TestCase):
    def test_migration_defines_immutable_per_feed_current_file_ledger(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "009_current_artifact_ledger.sql"
        )
        self.assertTrue(migration_path.exists(), "CURRENT ledger migration is missing")
        migration = migration_path.read_text(encoding="utf-8")

        required_fragments = (
            "CREATE TABLE IF NOT EXISTS current_artifact_ledger",
            "PRIMARY KEY (feed, source_artifact_id)",
            "CHECK (feed IN ('dispatch_scada', 'dispatch_price'))",
            "status IN ('pending', 'completed', 'missed')",
            "attempt_count BIGINT NOT NULL",
            "last_error TEXT",
            "first_seen_at TIMESTAMPTZ NOT NULL",
            "last_seen_at TIMESTAMPTZ NOT NULL",
            "completed_at TIMESTAMPTZ",
            "current_artifact_ledger_selection_idx",
            "CURRENT/Dispatch_SCADA/",
            "CURRENT/DispatchIS_Reports/",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, migration)
        self.assertNotIn("ARCHIVE", migration)

    def test_migration_deploys_after_archive_url_support_and_is_verified(self) -> None:
        app_root = Path(__file__).resolve().parents[2]
        migrate_script = (app_root / "deploy" / "migrate.sh").read_text(encoding="utf-8")

        self.assertIn("009_allow_archive_urls.sql", migrate_script)
        self.assertIn("009_current_artifact_ledger.sql", migrate_script)
        self.assertLess(
            migrate_script.index("009_allow_archive_urls.sql"),
            migrate_script.index("009_current_artifact_ledger.sql"),
        )
        self.assertIn("'current_artifact_ledger'", migrate_script)
        self.assertIn("SELECT count(*) = 14", migrate_script)
        self.assertEqual(migrate_script.count('--dbname="$database_url"'), 11)
        upper = (
            (app_root / "migrations" / "009_current_artifact_ledger.sql")
            .read_text(encoding="utf-8")
            .upper()
        )
        for forbidden in ("DROP ", "ALTER ", "TRUNCATE "):
            self.assertNotIn(forbidden, upper)
        self.assertNotRegex(upper, r"\bDELETE\s+FROM\b")


class PostgreSQLCurrentArtifactLedgerTests(unittest.TestCase):
    def test_reconcile_accepts_non_utc_offset_for_the_same_report_instant(self) -> None:
        filename = "PUBLIC_DISPATCHSCADA_202608291200_42.zip"
        reference = CurrentArtifactReference(
            "dispatch_scada",
            "42",
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
            filename,
            datetime(2026, 8, 29, 12, 0, tzinfo=timezone(timedelta(hours=10))),
        )
        connection = FakeConnection(fetchone_results=((1,),))

        result = PostgreSQLCurrentArtifactLedger(connection).reconcile((reference,))

        self.assertEqual(result, CurrentArtifactLedgerReconciliation(1, 1))

    def test_bounded_error_summary_is_trimmed_after_truncation(self) -> None:
        summary = bounded_error_summary("x" * 511 + " " + "tail")

        self.assertEqual(summary, "x" * 511)
        self.assertLessEqual(len(summary), 512)
        self.assertEqual(summary, summary.strip())

    def test_reconcile_records_one_new_current_reference_as_pending(self) -> None:
        filename = "PUBLIC_DISPATCHSCADA_202608291200_42.zip"
        reference = CurrentArtifactReference(
            feed="dispatch_scada",
            source_artifact_id="42",
            source_url=(
                "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
                + filename
            ),
            zip_filename=filename,
            report_timestamp=datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc),
        )
        connection = FakeConnection(fetchone_results=((1,),))

        result = PostgreSQLCurrentArtifactLedger(connection).reconcile((reference,))

        self.assertEqual(result, CurrentArtifactLedgerReconciliation(1, 1))
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual(connection.closed_cursors, 1)
        self.assertEqual(len(connection.executions), 1)
        statement, parameters = connection.executions[0]
        self.assertIn("INSERT INTO current_artifact_ledger", statement)
        self.assertIn("status", statement)
        self.assertEqual(parameters[:5], (
            reference.feed,
            reference.source_artifact_id,
            reference.source_url,
            reference.zip_filename,
            reference.report_timestamp,
        ))

    def test_reconcile_accepts_a_feed_and_source_adapter_references(self) -> None:
        filename = "PUBLIC_DISPATCHSCADA_202608291200_42.zip"
        source_reference = DispatchScadaArtifactRef(
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
            filename,
            "42",
            datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc),
        )
        connection = FakeConnection(fetchone_results=((1,),))

        result = PostgreSQLCurrentArtifactLedger(connection).reconcile(
            "dispatch_scada", (source_reference,)
        )

        self.assertEqual(result, CurrentArtifactLedgerReconciliation(1, 1))
        self.assertEqual(connection.executions[0][1][0], "dispatch_scada")

    def test_reconcile_rejects_filename_timestamp_identity_conflict(self) -> None:
        filename = "PUBLIC_DISPATCHSCADA_202608291200_42.zip"
        reference = CurrentArtifactReference(
            "dispatch_scada",
            "42",
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
            filename,
            datetime(2026, 8, 29, 2, 5, tzinfo=timezone.utc),
        )
        connection = FakeConnection(fetchone_results=((1,),))

        with self.assertRaises(ValueError):
            PostgreSQLCurrentArtifactLedger(connection).reconcile((reference,))

        self.assertEqual(connection.executions, [])

    def test_newly_reconciled_pending_row_is_immediately_claimable(self) -> None:
        filename = "PUBLIC_DISPATCHSCADA_202608291200_1.zip"
        reference = CurrentArtifactReference(
            "dispatch_scada",
            "1",
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
            filename,
            datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc),
        )
        connection = FakeConnection(
            fetchone_results=((1,), (1,)),
            fetchall_results=(
                [(
                    reference.feed,
                    reference.source_artifact_id,
                    reference.source_url,
                    reference.zip_filename,
                    reference.report_timestamp,
                    "pending",
                    0,
                )],
            ),
        )

        ledger = PostgreSQLCurrentArtifactLedger(connection)
        reconciliation = ledger.reconcile((reference,))
        claims = ledger.claim_pending(reference.feed, limit=1)

        self.assertEqual(reconciliation, CurrentArtifactLedgerReconciliation(1, 1))
        self.assertEqual(claims, (CurrentArtifactClaim(reference, 1),))
        selection_sql = connection.executions[1][0]
        self.assertIn("last_attempted_at IS NULL", selection_sql)

    def test_recently_attempted_pending_and_missed_rows_wait_for_retry_window(self) -> None:
        connection = FakeConnection(fetchall_results=([],))

        PostgreSQLCurrentArtifactLedger(connection).claim_pending(
            "dispatch_scada", limit=1
        )

        selection_sql = connection.executions[0][0]
        self.assertIn("status IN ('pending', 'missed')", selection_sql)
        self.assertIn("last_attempted_at IS NULL", selection_sql)
        self.assertIn(
            "last_attempted_at <= CURRENT_TIMESTAMP - INTERVAL '15 minutes'",
            selection_sql,
        )
        self.assertNotIn("attempt_count <", selection_sql)
        self.assertIn("FOR UPDATE OF ledger SKIP LOCKED", selection_sql)

    def test_claim_pending_is_chronological_and_bounded(self) -> None:
        older_filename = "PUBLIC_DISPATCHSCADA_202608291200_1.zip"
        newer_filename = "PUBLIC_DISPATCHSCADA_202608291205_2.zip"
        older = CurrentArtifactReference(
            "dispatch_scada",
            "1",
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + older_filename,
            older_filename,
            datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc),
        )
        newer = CurrentArtifactReference(
            "dispatch_scada",
            "2",
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + newer_filename,
            newer_filename,
            datetime(2026, 8, 29, 2, 5, tzinfo=timezone.utc),
        )
        connection = FakeConnection(
            fetchall_results=(
                [
                    (*(
                        older.feed,
                        older.source_artifact_id,
                        older.source_url,
                        older.zip_filename,
                        older.report_timestamp,
                    ), "missed", 3),
                    (*(
                        newer.feed,
                        newer.source_artifact_id,
                        newer.source_url,
                        newer.zip_filename,
                        newer.report_timestamp,
                    ), "pending", 0),
                ],
            ),
            fetchone_results=((4,), (1,)),
        )

        claims = PostgreSQLCurrentArtifactLedger(connection).claim_pending(
            "dispatch_scada", limit=2
        )

        self.assertEqual(
            claims,
            (
                CurrentArtifactClaim(older, 4),
                CurrentArtifactClaim(newer, 1),
            ),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        selection_sql = connection.executions[0][0]
        self.assertIn("status IN ('pending', 'missed')", selection_sql)
        self.assertIn("last_attempted_at IS NULL", selection_sql)
        self.assertIn(
            "last_attempted_at <= CURRENT_TIMESTAMP - INTERVAL '15 minutes'",
            selection_sql,
        )
        self.assertIn("ORDER BY report_timestamp ASC", selection_sql)
        self.assertIn("LIMIT %s", selection_sql)
        self.assertIn("ORDER BY report_timestamp DESC", selection_sql)
        self.assertIn("UNION", selection_sql)
        self.assertIn("source_url", selection_sql)
        self.assertEqual(connection.executions[0][1], ("dispatch_scada", 1, 1))

    def test_claim_pending_limit_one_selects_newest_eligible_artifact(self) -> None:
        class SelectionCursor(FakeCursor):
            def fetchall(self):
                rows = super().fetchall()
                _, parameters = self.connection.executions[0]
                oldest_limit, newest_limit = parameters[1:]
                ordered = sorted(rows, key=lambda row: row[4])
                selected = ordered[:oldest_limit]
                if newest_limit:
                    selected.extend(ordered[-newest_limit:])
                return selected

        class SelectionConnection(FakeConnection):
            def cursor(self):
                return SelectionCursor(self)

        older_filename = "PUBLIC_DISPATCHSCADA_202608291200_1.zip"
        newer_filename = "PUBLIC_DISPATCHSCADA_202608291205_2.zip"
        older = CurrentArtifactReference(
            "dispatch_scada",
            "1",
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + older_filename,
            older_filename,
            datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc),
        )
        newer = CurrentArtifactReference(
            "dispatch_scada",
            "2",
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + newer_filename,
            newer_filename,
            datetime(2026, 8, 29, 2, 5, tzinfo=timezone.utc),
        )
        connection = SelectionConnection(
            fetchall_results=(
                [
                    (
                        older.feed,
                        older.source_artifact_id,
                        older.source_url,
                        older.zip_filename,
                        older.report_timestamp,
                        "pending",
                        0,
                    ),
                    (
                        newer.feed,
                        newer.source_artifact_id,
                        newer.source_url,
                        newer.zip_filename,
                        newer.report_timestamp,
                        "pending",
                        0,
                    ),
                ],
            ),
            fetchone_results=((1,),),
        )

        claims = PostgreSQLCurrentArtifactLedger(connection).claim_pending(
            "dispatch_scada", limit=1
        )

        self.assertEqual(claims, (CurrentArtifactClaim(newer, 1),))
        self.assertEqual(connection.executions[0][1], ("dispatch_scada", 0, 1))

    def test_claim_pending_binds_safe_split_for_every_supported_limit(self) -> None:
        for limit in range(1, 13):
            with self.subTest(limit=limit):
                connection = FakeConnection(fetchall_results=([],))

                claims = PostgreSQLCurrentArtifactLedger(connection).claim_pending(
                    "dispatch_scada", limit=limit
                )

                self.assertEqual(claims, ())
                selection_sql, parameters = connection.executions[0]
                self.assertEqual(selection_sql.count("%s"), 3)
                self.assertEqual(
                    parameters,
                    ("dispatch_scada", limit // 2, limit - (limit // 2)),
                )

    def test_claim_pending_fairly_combines_oldest_and_newest_lanes(self) -> None:
        class FairSelectionCursor(FakeCursor):
            def fetchall(self):
                rows = super().fetchall()
                statement = self.connection.executions[-1][0]
                if "ORDER BY report_timestamp DESC" in statement:
                    return [rows[0], rows[1], rows[-2], rows[-1]]
                return rows[:4]

        class FairSelectionConnection(FakeConnection):
            def cursor(self):
                return FairSelectionCursor(self)

        references = tuple(
            CurrentArtifactReference(
                "dispatch_scada",
                str(index),
                "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
                + f"PUBLIC_DISPATCHSCADA_2026082912{index:02d}_{index}.zip",
                f"PUBLIC_DISPATCHSCADA_2026082912{index:02d}_{index}.zip",
                datetime(2026, 8, 29, 2, index, tzinfo=timezone.utc),
            )
            for index in (0, 5, 10, 15, 20, 25)
        )
        connection = FairSelectionConnection(
            fetchall_results=(
                [
                    (
                        reference.feed,
                        reference.source_artifact_id,
                        reference.source_url,
                        reference.zip_filename,
                        reference.report_timestamp,
                        "pending",
                        0,
                    )
                    for reference in references
                ],
            ),
            fetchone_results=((1,), (1,), (1,), (1,)),
        )

        claims = PostgreSQLCurrentArtifactLedger(connection).claim_pending(
            "dispatch_scada", limit=4
        )

        self.assertEqual(
            tuple(claim.reference for claim in claims),
            (references[0], references[1], references[-2], references[-1]),
        )
        self.assertEqual(connection.executions[0][1], ("dispatch_scada", 2, 2))

    def test_completed_rows_are_not_eligible_for_a_claim(self) -> None:
        connection = FakeConnection(fetchall_results=([],))

        claims = PostgreSQLCurrentArtifactLedger(connection).claim_pending(
            "dispatch_scada", limit=12
        )

        self.assertEqual(claims, ())
        self.assertIn("status IN ('pending', 'missed')", connection.executions[0][0])
        self.assertNotIn("'completed'", connection.executions[0][0])

    def test_miss_is_bounded_and_complete_clears_error_after_ingestion(self) -> None:
        filename = "PUBLIC_DISPATCHSCADA_202608291200_1.zip"
        reference = CurrentArtifactReference(
            "dispatch_scada",
            "1",
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
            filename,
            datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc),
        )
        claim = CurrentArtifactClaim(reference, 1)
        connection = FakeConnection(
            fetchone_results=(
                (
                    reference.source_url,
                    reference.zip_filename,
                    reference.report_timestamp,
                    "pending",
                    1,
                ),
                (1,),
            )
        )

        missed = PostgreSQLCurrentArtifactLedger(connection).miss(
            claim,
            error_summary="fetch failed " + ("x" * 1000),
        )

        self.assertEqual(
            missed,
            CurrentArtifactMiss(("fetch failed " + ("x" * 1000))[:512]),
        )
        miss_sql = connection.executions[1][0]
        self.assertIn("status = 'missed'", miss_sql)
        self.assertLessEqual(len(connection.executions[1][1][0]), 512)

        complete_connection = FakeConnection(
            fetchone_results=(
                (
                    reference.source_url,
                    reference.zip_filename,
                    reference.report_timestamp,
                    "missed",
                    1,
                ),
                (1,),
            )
        )
        completed = PostgreSQLCurrentArtifactLedger(complete_connection).complete(claim)
        self.assertEqual(completed, CurrentArtifactCompletion(False))
        self.assertIn("status = 'completed'", complete_connection.executions[1][0])
        self.assertIn("last_error = NULL", complete_connection.executions[1][0])


if __name__ == "__main__":
    unittest.main()
