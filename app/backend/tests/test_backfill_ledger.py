"""Contract tests for the resumable historical backfill ledger."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import unittest

from batterywatch_api.backfill_ledger import (
    BackfillClaim,
    BackfillEnsureResult,
    BackfillItemCompletion,
    BackfillItemFailure,
    BackfillPlanItem,
    BackfillRunConflictError,
    BackfillRunFinalization,
    BackfillRunProgress,
    BackfillRunSpec,
    PostgreSQLBackfillLedger,
)


UTC = timezone.utc


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

    def fetchall(self):
        if self.connection.fetchall_results:
            return self.connection.fetchall_results.pop(0)
        return []

    def close(self) -> None:
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(
        self, *, fetchone_results=(), fetchalls=(), fail_on_execute=None, failure=None
    ) -> None:
        self.executions = []
        self.fetchone_results = list(fetchone_results)
        self.fetchall_results = list(fetchalls)
        self.fail_on_execute = fail_on_execute
        self.failure = failure
        self.cursor_calls = self.closed_cursors = 0
        self.commits = self.rollbacks = 0

    def cursor(self):
        self.cursor_calls += 1
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class BackfillLedgerMigrationTests(unittest.TestCase):
    def test_deploys_additive_run_item_and_event_schema(self) -> None:
        app_root = Path(__file__).resolve().parents[2]
        migration_path = app_root / "migrations" / "004_historical_backfill_ledger.sql"
        self.assertTrue(migration_path.exists(), "004 historical ledger migration is missing")

        migration = migration_path.read_text(encoding="utf-8")
        migrate_script = (app_root / "deploy" / "migrate.sh").read_text(encoding="utf-8")
        upper = migration.upper()

        for table in (
            "historical_backfill_runs",
            "historical_backfill_items",
            "historical_backfill_events",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", migration)
            self.assertIn(f"'{table}'", migrate_script)

        self.assertIn("004_historical_backfill_ledger.sql", migrate_script)
        self.assertIn("SELECT count(*) = 15", migrate_script)
        self.assertEqual(migrate_script.count('--dbname="$database_url"'), 11)
        self.assertIn("ON DELETE RESTRICT", migration)
        self.assertIn("event_seq BIGSERIAL PRIMARY KEY", migration)
        self.assertIn("historical_backfill_items_claim_idx", migration)
        self.assertIn("historical_backfill_events_order_idx", migration)
        for forbidden in ("DROP ", "TRUNCATE ", "ALTER TABLE"):
            self.assertNotIn(forbidden, upper)
        self.assertNotRegex(upper, r"\bDELETE\s+FROM\b")


class PostgreSQLBackfillLedgerTests(unittest.TestCase):
    def test_progress_returns_deterministic_status_and_attempt_counts(self) -> None:
        connection = FakeConnection(
            fetchone_results=(("running", 62, 2, 1, 58, 1, 64),)
        )

        progress = PostgreSQLBackfillLedger(connection).progress("run-20260828")

        self.assertEqual(
            progress,
            BackfillRunProgress("run-20260828", "running", 62, 2, 1, 58, 1, 64),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))
        self.assertEqual(len(connection.executions), 1)
        statement, parameters = connection.executions[0]
        self.assertIn("FILTER (WHERE i.status = 'pending')", statement)
        self.assertIn("FILTER (WHERE i.status = 'running')", statement)
        self.assertIn("FILTER (WHERE i.status = 'completed')", statement)
        self.assertIn("FILTER (WHERE i.status = 'failed')", statement)
        self.assertIn("SUM(i.attempt_count)", statement)
        self.assertIn("COALESCE(SUM(i.attempt_count), 0)::bigint", statement)
        self.assertEqual(parameters, ("run-20260828",))

    def test_progress_rejects_inconsistent_database_counts(self) -> None:
        connection = FakeConnection(
            fetchone_results=(("running", 62, 2, 1, 58, 0, 64),)
        )

        with self.assertRaises(BackfillRunConflictError):
            PostgreSQLBackfillLedger(connection).progress("run-20260828")

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))

    def test_finalize_completes_run_with_deterministic_progress(self) -> None:
        connection = FakeConnection(
            fetchone_results=(
                ("running",),
                ("running", 2, 0, 0, 2, 0, 3),
                (1,),
            )
        )

        result = PostgreSQLBackfillLedger(connection).finalize("run-20260828")

        self.assertEqual(
            result,
            BackfillRunFinalization(
                False,
                BackfillRunProgress(
                    "run-20260828", "completed", 2, 0, 0, 2, 0, 3
                ),
            ),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))
        self.assertEqual(len(connection.executions), 3)
        self.assertIn("FOR UPDATE", connection.executions[0][0])
        self.assertIn("FILTER (WHERE i.status = 'completed')", connection.executions[1][0])
        self.assertIn("status = 'completed'", connection.executions[2][0])
        self.assertEqual(connection.executions[2][1], ("run-20260828",))

    def test_finalize_rejects_incomplete_run_without_update(self) -> None:
        connection = FakeConnection(
            fetchone_results=(
                ("running",),
                ("running", 2, 1, 0, 1, 0, 2),
                None,
            )
        )

        with self.assertRaisesRegex(BackfillRunConflictError, "incomplete"):
            PostgreSQLBackfillLedger(connection).finalize("run-20260828")

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual(len(connection.executions), 2)

    def test_finalize_exact_replay_is_idempotent_without_update(self) -> None:
        connection = FakeConnection(
            fetchone_results=(
                ("completed",),
                ("completed", 2, 0, 0, 2, 0, 3),
            )
        )

        result = PostgreSQLBackfillLedger(connection).finalize("run-20260828")

        self.assertTrue(result.replayed)
        self.assertEqual(result.progress.status, "completed")
        self.assertEqual(result.progress.completed, 2)
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual(len(connection.executions), 2)

    def test_finalize_rejects_missing_guarded_run_update(self) -> None:
        connection = FakeConnection(
            fetchone_results=(
                ("running",),
                ("running", 1, 0, 0, 1, 0, 1),
                None,
            )
        )

        with self.assertRaisesRegex(BackfillRunConflictError, "not applied"):
            PostgreSQLBackfillLedger(connection).finalize("run-20260828")

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual(len(connection.executions), 3)

    def test_fail_records_guarded_item_transition_and_event(self) -> None:
        claim = BackfillClaim(
            "run-20260828",
            "dispatch_scada",
            date(2026, 8, 28),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
            "PUBLIC_DISPATCHSCADA_20260828.zip",
            2,
        )
        connection = FakeConnection(
            fetchone_results=(
                (claim.source_url, "running", claim.attempt_number),
                (1,),
            )
        )

        result = PostgreSQLBackfillLedger(connection).fail(
            claim, error_summary="archive checksum mismatch"
        )

        self.assertEqual(
            result, BackfillItemFailure(False, "archive checksum mismatch")
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))
        self.assertEqual(len(connection.executions), 3)
        self.assertIn("FOR UPDATE", connection.executions[0][0])
        self.assertIn("status = 'failed'", connection.executions[1][0])
        self.assertIn("status = 'running'", connection.executions[1][0])
        self.assertEqual(
            connection.executions[1][1],
            (
                "archive checksum mismatch",
                claim.run_id,
                claim.feed,
                claim.report_date,
                claim.attempt_number,
            ),
        )
        self.assertEqual(
            connection.executions[2][1],
            (
                claim.run_id,
                claim.feed,
                claim.report_date,
                "failed",
                claim.attempt_number,
                "archive checksum mismatch",
            ),
        )

    def test_fail_rejects_invalid_error_summary_before_sql(self) -> None:
        claim = BackfillClaim(
            "run-20260828",
            "dispatch_scada",
            date(2026, 8, 28),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
            "PUBLIC_DISPATCHSCADA_20260828.zip",
            1,
        )
        connection = FakeConnection()

        with self.assertRaises(ValueError):
            PostgreSQLBackfillLedger(connection).fail(claim, error_summary="")

        self.assertEqual(
            (connection.executions, connection.cursor_calls,
             connection.commits, connection.rollbacks),
            ([], 0, 0, 0),
        )

    def test_complete_records_guarded_item_transition_and_event(self) -> None:
        claim = BackfillClaim(
            "run-20260828",
            "dispatch_price",
            date(2026, 8, 28),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260828.zip",
            4,
        )
        connection = FakeConnection(
            fetchone_results=(
                (claim.source_url, "running", claim.attempt_number),
                (1,),
            )
        )

        result = PostgreSQLBackfillLedger(connection).complete(
            claim, records_imported=145
        )

        self.assertEqual(result, BackfillItemCompletion(False, 145))
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))
        self.assertEqual(len(connection.executions), 3)
        self.assertIn("FOR UPDATE", connection.executions[0][0])
        self.assertIn("status = 'completed'", connection.executions[1][0])
        self.assertIn("status = 'running'", connection.executions[1][0])
        self.assertEqual(
            connection.executions[1][1],
            (claim.run_id, claim.feed, claim.report_date, claim.attempt_number),
        )
        self.assertEqual(
            connection.executions[2][1],
            (
                claim.run_id,
                claim.feed,
                claim.report_date,
                "completed",
                claim.attempt_number,
                145,
            ),
        )

    def test_complete_rejects_invalid_claim_before_sql(self) -> None:
        claim = BackfillClaim(
            "run-20260828",
            "dispatch_price",
            date(2026, 8, 28),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260828.zip",
            1,
        )
        connection = FakeConnection()

        with self.assertRaises(ValueError):
            PostgreSQLBackfillLedger(connection).complete(
                replace(claim, source_url="https://example.invalid/archive.zip"),
                records_imported=0,
            )

        self.assertEqual(
            (connection.executions, connection.cursor_calls,
             connection.commits, connection.rollbacks),
            ([], 0, 0, 0),
        )

    def test_claim_next_claims_deterministic_item_and_appends_event(self) -> None:
        run_id = "run-20260828"
        report_date = date(2026, 8, 28)
        source_url = (
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260828.zip"
        )
        connection = FakeConnection(
            fetchone_results=(
                ("running",),
                ("dispatch_price", report_date, source_url),
                ("dispatch_price", report_date, source_url, 4),
            )
        )

        claim = PostgreSQLBackfillLedger(connection).claim_next(run_id)

        self.assertEqual(
            claim,
            BackfillClaim(run_id, "dispatch_price", report_date, source_url, 4),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))
        self.assertEqual(len(connection.executions), 4)
        self.assertIn("FROM historical_backfill_runs", connection.executions[0][0])
        self.assertIn("status IN ('pending', 'failed')", connection.executions[1][0])
        self.assertIn("ORDER BY feed, report_date", connection.executions[1][0])
        self.assertIn("FOR UPDATE SKIP LOCKED", connection.executions[1][0])
        self.assertIn("UPDATE historical_backfill_items", connection.executions[2][0])
        self.assertEqual(
            connection.executions[2][1], (run_id, "dispatch_price", report_date)
        )
        self.assertEqual(
            connection.executions[3][1],
            (run_id, "dispatch_price", report_date, "claimed", 4),
        )

    def test_claim_next_database_failure_rolls_back_and_reraises_same_error(self) -> None:
        report_date = date(2026, 8, 28)
        source_url = (
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260828.zip"
        )
        failure = RuntimeError("injected database failure")
        connection = FakeConnection(
            fetchone_results=(("running",),),
            fail_on_execute=2,
            failure=failure,
        )

        with self.assertRaises(RuntimeError) as raised:
            PostgreSQLBackfillLedger(connection).claim_next("run-20260828")

        self.assertIs(raised.exception, failure)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual(connection.closed_cursors, 1)

    def test_claim_next_rejects_invalid_run_id_before_sql(self) -> None:
        connection = FakeConnection()

        with self.assertRaises(ValueError):
            PostgreSQLBackfillLedger(connection).claim_next("bad id")

        self.assertEqual(
            (connection.executions, connection.cursor_calls,
             connection.commits, connection.rollbacks),
            ([], 0, 0, 0),
        )

    def test_claim_next_returns_none_when_no_item_is_eligible(self) -> None:
        connection = FakeConnection(fetchone_results=(("running",), None))

        claim = PostgreSQLBackfillLedger(connection).claim_next("run-20260828")

        self.assertIsNone(claim)
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))
        self.assertEqual(len(connection.executions), 2)
        self.assertFalse(
            any(
                "INSERT INTO historical_backfill_events" in statement
                for statement, _ in connection.executions
            )
        )

    def test_claim_next_rejects_absent_run(self) -> None:
        connection = FakeConnection(fetchone_results=(None,))

        with self.assertRaises(BackfillRunConflictError):
            PostgreSQLBackfillLedger(connection).claim_next("missing-run")

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))

    def test_ensure_new_run_plans_items_and_events_in_deterministic_order(self) -> None:
        spec = BackfillRunSpec(
            "run-20260828",
            datetime(2026, 8, 27, 14, tzinfo=UTC),
            datetime(2026, 8, 29, 14, tzinfo=UTC),
            1,
        )
        items = (
            BackfillPlanItem(
                "dispatch_scada",
                date(2026, 8, 28),
                "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
                "PUBLIC_DISPATCHSCADA_20260828.zip",
            ),
            BackfillPlanItem(
                "dispatch_price",
                date(2026, 8, 28),
                "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
                "PUBLIC_DISPATCHIS_20260828.zip",
            ),
        )
        connection = FakeConnection(fetchone_results=((1,),))

        result = PostgreSQLBackfillLedger(connection).ensure_run(spec, items)

        self.assertEqual(result, BackfillEnsureResult(True, False, 2, 0))
        self.assertEqual(
            (connection.cursor_calls, connection.closed_cursors,
             connection.commits, connection.rollbacks),
            (1, 1, 1, 0),
        )
        self.assertEqual(len(connection.executions), 5)
        self.assertIn("INSERT INTO historical_backfill_runs", connection.executions[0][0])
        self.assertEqual(
            tuple(parameters[1] for statement, parameters in connection.executions
                  if "INSERT INTO historical_backfill_items" in statement),
            ("dispatch_price", "dispatch_scada"),
        )
        self.assertEqual(
            tuple(parameters[3] for statement, parameters in connection.executions
                  if "INSERT INTO historical_backfill_events" in statement),
            ("planned", "planned"),
        )
        self.assertTrue(all("%s" in statement for statement, _ in connection.executions))

    def test_ensure_new_run_accepts_canonical_monthly_nextday_soc_item(self) -> None:
        spec = BackfillRunSpec(
            "soc-run-202507",
            datetime(2025, 7, 1, tzinfo=UTC),
            datetime(2025, 8, 1, tzinfo=UTC),
            3,
        )
        item = BackfillPlanItem(
            "nextday_soc",
            date(2025, 7, 1),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Next_Day_Dispatch/"
            "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
        )
        connection = FakeConnection(fetchone_results=((1,),))

        result = PostgreSQLBackfillLedger(connection).ensure_run(spec, (item,))

        self.assertEqual(result, BackfillEnsureResult(True, False, 1, 0))
        item_inserts = tuple(
            parameters
            for statement, parameters in connection.executions
            if "INSERT INTO historical_backfill_items" in statement
        )
        self.assertEqual(
            item_inserts,
            ((spec.run_id, item.feed, item.report_date, item.source_url),),
        )

    def test_monthly_nextday_soc_item_requires_first_day_identity(self) -> None:
        spec = BackfillRunSpec(
            "soc-run-202507-bad",
            datetime(2025, 7, 1, tzinfo=UTC),
            datetime(2025, 8, 1, tzinfo=UTC),
            3,
        )
        item = BackfillPlanItem(
            "nextday_soc",
            date(2025, 7, 2),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Next_Day_Dispatch/"
            "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
        )
        connection = FakeConnection()

        with self.assertRaisesRegex(ValueError, "monthly"):
            PostgreSQLBackfillLedger(connection).ensure_run(spec, (item,))

        self.assertEqual(connection.cursor_calls, 0)

    def test_exact_resume_recovers_interrupted_item_and_preserves_attempt(self) -> None:
        start = datetime(2026, 8, 27, 14, tzinfo=UTC)
        end = datetime(2026, 8, 29, 14, tzinfo=UTC)
        spec = BackfillRunSpec("run-20260828", start, end, 1)
        price = BackfillPlanItem(
            "dispatch_price",
            date(2026, 8, 28),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260828.zip",
        )
        scada = BackfillPlanItem(
            "dispatch_scada",
            date(2026, 8, 28),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
            "PUBLIC_DISPATCHSCADA_20260828.zip",
        )
        connection = FakeConnection(
            fetchone_results=(None, (spec.run_id, start, end, 1)),
            fetchalls=(
                (
                    (price.feed, price.report_date, price.source_url),
                    (scada.feed, scada.report_date, scada.source_url),
                ),
                ((scada.feed, scada.report_date, 3),),
            ),
        )

        result = PostgreSQLBackfillLedger(connection).ensure_run(spec, (scada, price))

        self.assertEqual(result, BackfillEnsureResult(False, True, 2, 1))
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual((connection.cursor_calls, connection.closed_cursors), (1, 1))
        statements = tuple(statement for statement, _ in connection.executions)
        self.assertIn("FROM historical_backfill_runs", statements[1])
        self.assertIn("FOR UPDATE", statements[1])
        self.assertIn("FROM historical_backfill_items", statements[2])
        self.assertIn("UPDATE historical_backfill_items", statements[3])
        self.assertIn("INSERT INTO historical_backfill_events", statements[4])
        self.assertEqual(connection.executions[4][1][-2:], ("recovered", 3))
        self.assertIn("UPDATE historical_backfill_runs", statements[5])

    def test_changed_existing_run_identity_fails_closed(self) -> None:
        start = datetime(2026, 8, 27, 14, tzinfo=UTC)
        end = datetime(2026, 8, 29, 14, tzinfo=UTC)
        spec = BackfillRunSpec("run-20260828", start, end, 1)
        item = BackfillPlanItem(
            "dispatch_price",
            date(2026, 8, 28),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260828.zip",
        )
        cases = (
            (
                "run",
                (spec.run_id, start, end, 2),
                ((item.feed, item.report_date, item.source_url),),
            ),
            (
                "plan",
                (spec.run_id, start, end, 1),
                (),
            ),
        )

        for label, stored_run, stored_items in cases:
            with self.subTest(label=label):
                connection = FakeConnection(
                    fetchone_results=(None, stored_run),
                    fetchalls=(stored_items, ()),
                )
                with self.assertRaises(BackfillRunConflictError):
                    PostgreSQLBackfillLedger(connection).ensure_run(spec, (item,))
                self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
                self.assertEqual(connection.closed_cursors, 1)

    def test_invalid_inputs_fail_before_sql(self) -> None:
        start = datetime(2026, 8, 27, 14, tzinfo=UTC)
        end = datetime(2026, 8, 29, 14, tzinfo=UTC)
        spec = BackfillRunSpec("run-20260828", start, end, 1)
        item = BackfillPlanItem(
            "dispatch_price",
            date(2026, 8, 28),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260828.zip",
        )
        cases = (
            (replace(spec, run_id="bad id"), (item,)),
            (replace(spec, requested_start=start.replace(tzinfo=None)), (item,)),
            (replace(spec, requested_end=start), (item,)),
            (replace(spec, requested_end=start + timedelta(days=367)), (item,)),
            (replace(spec, ingestion_version=-1), (item,)),
            (replace(spec, ingestion_version=1 << 63), (item,)),
            (spec, ()),
            (spec, (replace(item, feed="regional_soc"),)),
            (spec, (replace(item, report_date=datetime(2026, 8, 28)),)),
            (spec, (replace(item, source_url="https://example.invalid/archive.zip"),)),
            (spec, (item, item)),
        )

        for invalid_spec, invalid_items in cases:
            with self.subTest(spec=invalid_spec, items=invalid_items):
                connection = FakeConnection()
                with self.assertRaises(ValueError):
                    PostgreSQLBackfillLedger(connection).ensure_run(
                        invalid_spec, invalid_items
                    )
                self.assertEqual(
                    (connection.executions, connection.commits, connection.rollbacks),
                    ([], 0, 0),
                )

    def test_database_failure_rolls_back_and_reraises_same_error(self) -> None:
        spec = BackfillRunSpec(
            "run-20260828",
            datetime(2026, 8, 27, 14, tzinfo=UTC),
            datetime(2026, 8, 29, 14, tzinfo=UTC),
            1,
        )
        item = BackfillPlanItem(
            "dispatch_price",
            date(2026, 8, 28),
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
            "PUBLIC_DISPATCHIS_20260828.zip",
        )
        failure = RuntimeError("injected database failure")
        connection = FakeConnection(
            fetchone_results=((1,),), fail_on_execute=3, failure=failure
        )

        with self.assertRaises(RuntimeError) as raised:
            PostgreSQLBackfillLedger(connection).ensure_run(spec, (item,))

        self.assertIs(raised.exception, failure)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertEqual(connection.closed_cursors, 1)


if __name__ == "__main__":
    unittest.main()
