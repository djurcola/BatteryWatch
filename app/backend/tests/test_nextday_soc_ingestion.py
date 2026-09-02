"""Tests for atomic authoritative individual Next Day SOC ingestion."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from batterywatch_api.battery_assets import load_battery_assets
from batterywatch_api.nextday_soc import parse_nextday_unit_solution_soc
from batterywatch_api.nextday_soc_ingestion import (
    NextDaySocConflictError,
    NextDaySocIngestionResult,
    PostgreSQLNextDaySocIngestor,
)

UTC = timezone.utc
BACKEND = Path(__file__).resolve().parents[1]
FIXTURE = BACKEND / "tests" / "fixtures" / "historical" / "nextday-unit-solution-soc-20260829-reduced.csv"
ASSETS = BACKEND.parent / "config" / "battery_assets.json"
ARTIFACT_SHA = "d7a2abdd2947ed4b222166b9f60e3a8052838190027dd9ce03cb291ba2d29bc4"
DOWNLOADED_AT = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.query_kind = ""
        self.rowcount = -1

    def execute(self, statement, parameters) -> None:
        self.connection.operation_count += 1
        if self.connection.operation_count == self.connection.fail_on_operation:
            raise self.connection.failure
        self.connection.executions.append((statement, tuple(parameters)))
        if "FROM historical_source_artifacts" in statement:
            self.query_kind = "artifact"
        elif "FROM raw_nextday_soc_observations" in statement:
            self.query_kind = "raw"
        elif "FROM generator_soc_5m" in statement:
            self.query_kind = "effective"
        else:
            self.query_kind = ""

    def executemany(self, statement, parameters) -> None:
        self.connection.operation_count += 1
        if self.connection.operation_count == self.connection.fail_on_operation:
            raise self.connection.failure
        materialized = tuple(tuple(item) for item in parameters)
        self.connection.bulk_executions.append((statement, materialized))
        if self.connection.bulk_rowcounts:
            self.rowcount = self.connection.bulk_rowcounts.pop(0)
        else:
            self.rowcount = len(materialized)

    def fetchone(self):
        if self.query_kind == "artifact":
            return self.connection.artifact_row
        return None

    def fetchall(self):
        if self.query_kind == "raw":
            return list(self.connection.raw_rows)
        if self.query_kind == "effective":
            if self.connection.effective_fetch_results:
                return list(self.connection.effective_fetch_results.pop(0))
            return list(self.connection.effective_rows)
        return []

    def close(self) -> None:
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(
        self,
        *,
        artifact_row=("nextday_soc",),
        raw_rows=(),
        effective_rows=(),
        fail_on_operation=None,
        failure=None,
        rollback_failure=None,
        bulk_rowcounts=(),
        effective_fetch_results=(),
    ) -> None:
        self.artifact_row = artifact_row
        self.raw_rows = tuple(raw_rows)
        self.effective_rows = tuple(effective_rows)
        self.executions = []
        self.bulk_executions = []
        self.fail_on_operation = fail_on_operation
        self.failure = failure
        self.rollback_failure = rollback_failure
        self.operation_count = 0
        self.bulk_rowcounts = list(bulk_rowcounts)
        self.effective_fetch_results = list(effective_fetch_results)
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


def observations():
    return parse_nextday_unit_solution_soc(
        FIXTURE.read_text(encoding="utf-8"),
        duids=frozenset(("ADPBA1", "KEPBG1")),
        source_artifact_id=ARTIFACT_SHA,
        downloaded_at=DOWNLOADED_AT,
        ingestion_version=8,
        correction_version=2,
    )


def assets():
    return tuple(
        asset
        for asset in load_battery_assets(ASSETS)
        if asset.duid in {"ADPBA1", "KEPBG1"}
    )


class PostgreSQLNextDaySocIngestorTests(unittest.TestCase):
    def test_ingests_real_fixture_raw_and_capacity_qualified_effective_rows(self) -> None:
        connection = FakeConnection()

        result = PostgreSQLNextDaySocIngestor(connection).ingest(
            observations(),
            assets(),
        )

        self.assertEqual(
            result,
            NextDaySocIngestionResult(
                source_rows=3,
                raw_inserted=3,
                raw_replayed=0,
                effective_candidates=3,
                effective_applied=3,
                effective_replayed=0,
                source_null_count=1,
                percentage_count=2,
            ),
        )
        self.assertEqual(
            (connection.cursor_calls, connection.closed_cursors),
            (1, 1),
        )
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))
        self.assertEqual(len(connection.executions), 3)
        self.assertEqual(len(connection.bulk_executions), 2)

        raw_sql, raw_parameters = connection.bulk_executions[0]
        self.assertIn("INSERT INTO raw_nextday_soc_observations", raw_sql)
        self.assertEqual(len(raw_parameters), 3)
        self.assertEqual({item[1] for item in raw_parameters}, {"ADPBA1", "KEPBG1"})
        self.assertIn(None, {item[3] for item in raw_parameters})

        effective_sql, effective_parameters = connection.bulk_executions[1]
        self.assertIn("INSERT INTO generator_soc_5m", effective_sql)
        self.assertEqual(len(effective_parameters), 3)
        adp_rows = [item for item in effective_parameters if item[0] == "ADPBA1"]
        kep_row = next(item for item in effective_parameters if item[0] == "KEPBG1")
        self.assertEqual(len(adp_rows), 2)
        for row in adp_rows:
            self.assertAlmostEqual(row[2], 100.0 * row[8] / 12.6)
            self.assertEqual(row[9], 12.6)
            self.assertEqual(row[12], "aemo-generation-information-2025")
            self.assertEqual(row[19], ARTIFACT_SHA)
        self.assertIsNone(kep_row[2])
        self.assertIsNone(kep_row[8])
        self.assertIsNone(kep_row[9])
        self.assertEqual(kep_row[7], ["authoritative_soc_missing"])

    def test_exact_raw_and_effective_replay_is_a_noop(self) -> None:
        source = observations()
        reviewed_assets = assets()
        primer = FakeConnection()
        PostgreSQLNextDaySocIngestor(primer).ingest(source, reviewed_assets)
        raw_rows = primer.bulk_executions[0][1]
        effective_rows = primer.bulk_executions[1][1]
        connection = FakeConnection(raw_rows=raw_rows, effective_rows=effective_rows)

        result = PostgreSQLNextDaySocIngestor(connection).ingest(
            source,
            reviewed_assets,
        )

        self.assertEqual(result.raw_inserted, 0)
        self.assertEqual(result.raw_replayed, 3)
        self.assertEqual(result.effective_applied, 0)
        self.assertEqual(result.effective_replayed, 3)
        self.assertEqual(connection.bulk_executions, [])
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))

    def test_conflicting_raw_replay_rolls_back(self) -> None:
        source = observations()
        primer = FakeConnection()
        PostgreSQLNextDaySocIngestor(primer).ingest(source, assets())
        stored = list(primer.bulk_executions[0][1])
        stored[0] = stored[0][:3] + (999.0,) + stored[0][4:]
        connection = FakeConnection(raw_rows=stored)

        with self.assertRaises(NextDaySocConflictError):
            PostgreSQLNextDaySocIngestor(connection).ingest(source, assets())

        self.assertEqual(connection.bulk_executions, [])
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))

    def test_intervention_wins_and_equal_precedence_conflict_fails_closed(self) -> None:
        base = observations()[0]
        normal = replace(base, soc_mwh=1.0, intervention=0)
        intervened = replace(base, soc_mwh=2.0, intervention=1)
        connection = FakeConnection()

        result = PostgreSQLNextDaySocIngestor(connection).ingest(
            (normal, intervened),
            assets(),
        )

        self.assertEqual(result.effective_candidates, 1)
        self.assertEqual(connection.bulk_executions[1][1][0][8], 2.0)

        conflict = replace(normal, run_number=2, soc_mwh=3.0)
        invalid_connection = FakeConnection()
        with self.assertRaises(NextDaySocConflictError):
            PostgreSQLNextDaySocIngestor(invalid_connection).ingest(
                (normal, conflict),
                assets(),
            )
        self.assertEqual(invalid_connection.executions, [])

    def test_future_capacity_and_source_above_capacity_preserve_mwh_without_percent(self) -> None:
        source = observations()[0]
        asset = next(item for item in assets() if item.duid == source.duid)
        cases = (
            (
                source,
                replace(
                    asset,
                    source_timestamp=source.interval_start + timedelta(seconds=1),
                ),
                "capacity_not_effective",
            ),
            (
                replace(source, soc_mwh=asset.storage_capacity_mwh + 1.0),
                asset,
                "soc_exceeds_capacity",
            ),
        )
        for observation, reviewed_asset, flag in cases:
            with self.subTest(flag=flag):
                connection = FakeConnection()
                PostgreSQLNextDaySocIngestor(connection).ingest(
                    (observation,),
                    (reviewed_asset,),
                )
                row = connection.bulk_executions[1][1][0]
                self.assertIsNone(row[2])
                self.assertEqual(row[8], observation.soc_mwh)
                self.assertIsNone(row[9])
                self.assertEqual(row[7], [flag])

    def test_stale_effective_revision_cannot_regress(self) -> None:
        source = (observations()[0],)
        primer = FakeConnection()
        PostgreSQLNextDaySocIngestor(primer).ingest(source, assets())
        stored = list(primer.bulk_executions[1][1][0])
        stored[6] += 1
        connection = FakeConnection(effective_rows=(tuple(stored),))

        result = PostgreSQLNextDaySocIngestor(connection).ingest(source, assets())

        self.assertEqual(result.effective_applied, 0)
        self.assertEqual(result.effective_replayed, 1)
        self.assertEqual(len(connection.bulk_executions), 1)
        self.assertIn("raw_nextday_soc_observations", connection.bulk_executions[0][0])

    def test_authoritative_row_replaces_legacy_row_with_null_revision_metadata(self) -> None:
        source = (observations()[0],)
        observation = source[0]
        legacy_row = (
            observation.duid,
            observation.interval_start,
            50.0,
            "legacy-fixture",
            observation.last_changed,
            0,
            0,
            ["legacy"],
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        connection = FakeConnection(effective_rows=(legacy_row,))

        result = PostgreSQLNextDaySocIngestor(connection).ingest(source, assets())

        self.assertEqual(result.effective_applied, 1)
        effective_sql = connection.bulk_executions[1][0]
        self.assertIn("generator_soc_5m.source_artifact_sha256 IS NULL", effective_sql)

    def test_post_prefetch_equal_precedence_conflict_fails_closed(self) -> None:
        source = (observations()[0],)
        primer = FakeConnection()
        PostgreSQLNextDaySocIngestor(primer).ingest(source, assets())
        conflicting = list(primer.bulk_executions[1][1][0])
        conflicting[8] = 999.0
        connection = FakeConnection(
            bulk_rowcounts=(1, 0),
            effective_fetch_results=((), (tuple(conflicting),)),
        )

        with self.assertRaises(NextDaySocConflictError):
            PostgreSQLNextDaySocIngestor(connection).ingest(source, assets())

        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))

    def test_invalid_inputs_fail_before_sql(self) -> None:
        source = observations()
        cases = (
            (source + (source[0],), assets()),
            ((replace(source[0], source_artifact_id="b" * 64), source[1]), assets()),
            ((replace(source[0], duid="UNKNOWN1"),), assets()),
        )
        for invalid_source, reviewed_assets in cases:
            with self.subTest(duids=[item.duid for item in invalid_source]):
                connection = FakeConnection()
                with self.assertRaises(ValueError):
                    PostgreSQLNextDaySocIngestor(connection).ingest(
                        invalid_source,
                        reviewed_assets,
                    )
                self.assertEqual(connection.executions, [])

    def test_sql_failure_rolls_back_and_preserves_original_error(self) -> None:
        failure = RuntimeError("raw bulk write failed")
        connection = FakeConnection(
            fail_on_operation=4,
            failure=failure,
            rollback_failure=RuntimeError("rollback also failed"),
        )
        same_error = False

        try:
            PostgreSQLNextDaySocIngestor(connection).ingest(observations(), assets())
        except RuntimeError as raised:
            same_error = raised is failure
        else:
            self.fail("expected raw bulk write failure")

        self.assertTrue(same_error)
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))


if __name__ == "__main__":
    unittest.main()
