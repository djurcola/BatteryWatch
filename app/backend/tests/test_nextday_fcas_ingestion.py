"""Tests for grouped FCAS persistence from retained Next Day observations."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import unittest

from psycopg.types.json import Jsonb

from batterywatch_api.nextday_soc import (
    NextDayFcasObservation,
    parse_nextday_unit_solution_soc,
)
from batterywatch_api.nextday_fcas_ingestion import (
    NextDayFcasConflictError,
    NextDayFcasIngestionResult,
    PostgreSQLNextDayFcasIngestor,
)

UTC = timezone.utc
BACKEND = Path(__file__).resolve().parents[1]
FIXTURE = BACKEND / "tests" / "fixtures" / "historical" / "nextday-unit-solution-soc-20260829-reduced.csv"
ARTIFACT_SHA = "d7a2abdd2947ed4b222166b9f60e3a8052838190027dd9ce03cb291ba2d29bc4"


class FakeCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.query_kind = ""
        self.parameters: tuple[Any, ...] = ()
        self.rowcount = -1

    def execute(self, statement, parameters) -> None:
        self.parameters = tuple(parameters)
        self.connection.executions.append((statement, self.parameters))
        if "FROM historical_source_artifacts" in statement:
            self.query_kind = "artifact"
        elif "FROM raw_nextday_fcas_observations" in statement:
            self.query_kind = "raw"
        elif "FROM generator_fcas_5m" in statement:
            self.query_kind = "effective"
        else:
            self.query_kind = ""

    def executemany(self, statement, parameters) -> None:
        rows = tuple(tuple(item) for item in parameters)
        self.connection.bulk_executions.append((statement, rows))
        if "raw_nextday_fcas_observations" in statement:
            existing = {
                (
                    row[0],
                    row[1],
                    row[2],
                    row[4],
                    row[5],
                    row[10],
                    row[11],
                )
                for row in self.connection.raw_rows
            }
            for row in rows:
                key = (
                    row[0], row[1], row[2], row[4], row[5], row[10], row[11]
                )
                if key in existing:
                    raise AssertionError("raw duplicate was not preflighted")
                self.connection.raw_rows.append(row)
                existing.add(key)
            self.rowcount = len(rows)
        elif "generator_fcas_5m" in statement:
            applied = 0
            for row in rows:
                key = (row[0], row[1])
                candidate_precedence = (
                    row[6], row[10], row[9], row[4], row[11]
                )
                for index, stored in enumerate(self.connection.effective_rows):
                    if (stored[0], stored[1]) != key:
                        continue
                    stored_precedence = (
                        stored[6], stored[10], stored[9], stored[4], stored[11]
                    )
                    if candidate_precedence > stored_precedence:
                        self.connection.effective_rows[index] = row
                        applied += 1
                    break
                else:
                    self.connection.effective_rows.append(row)
                    applied += 1
            self.rowcount = applied
        else:
            raise AssertionError("unexpected bulk statement")

    def fetchone(self):
        if self.query_kind == "artifact":
            return self.connection.artifact_row
        return None

    def fetchall(self):
        if self.query_kind == "raw":
            artifact, ingestion, correction = self.parameters
            return [
                row
                for row in self.connection.raw_rows
                if row[0] == artifact
                and row[10] == ingestion
                and row[11] == correction
            ]
        if self.query_kind == "effective":
            duids, start, end = self.parameters
            return [
                row
                for row in self.connection.effective_rows
                if row[0] in duids and start <= row[1] <= end
            ]
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
    ) -> None:
        self.artifact_row = artifact_row
        self.executions = []
        self.bulk_executions = []
        self.raw_rows = list(raw_rows)
        self.effective_rows = list(effective_rows)
        self.closed_cursors = 0
        self.commits = self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def observation():
    return parse_nextday_unit_solution_soc(
        FIXTURE.read_text(encoding="utf-8"),
        duids=frozenset(("ADPBA1",)),
        source_artifact_id=ARTIFACT_SHA,
        downloaded_at=datetime(2026, 8, 30, tzinfo=UTC),
        ingestion_version=8,
        correction_version=2,
    )[0]


class PostgreSQLNextDayFcasIngestorTests(unittest.TestCase):
    def test_one_observation_creates_one_grouped_raw_and_effective_record(self) -> None:
        connection = FakeConnection()

        result = PostgreSQLNextDayFcasIngestor(connection).ingest((observation(),))

        self.assertEqual(
            result,
            NextDayFcasIngestionResult(1, 1, 0, 1, 1, 0, 10),
        )
        self.assertEqual(len(connection.bulk_executions), 2)
        raw_sql, raw_rows = connection.bulk_executions[0]
        effective_sql, effective_rows = connection.bulk_executions[1]
        self.assertIn("INSERT INTO raw_nextday_fcas_observations", raw_sql)
        self.assertIn("INSERT INTO generator_fcas_5m", effective_sql)
        self.assertEqual(len(raw_rows), 1)
        self.assertEqual(len(effective_rows), 1)
        self.assertEqual(len(raw_rows[0][3].obj), 10)
        self.assertEqual(len(effective_rows[0][2].obj), 10)
        self.assertEqual(raw_rows[0][3].obj["raise_6s"]["target_mw"], 3.0)
        self.assertEqual(effective_rows[0][2].obj, raw_rows[0][3].obj)
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))

    def test_exact_replay_is_a_noop(self) -> None:
        source = observation()
        connection = FakeConnection()
        ingestor = PostgreSQLNextDayFcasIngestor(connection)
        ingestor.ingest((source,))

        result = ingestor.ingest((source,))

        self.assertEqual(result.raw_inserted, 0)
        self.assertEqual(result.raw_replayed, 1)
        self.assertEqual(result.effective_applied, 0)
        self.assertEqual(result.effective_replayed, 1)
        self.assertEqual(len(connection.raw_rows), 1)
        self.assertEqual(len(connection.effective_rows), 1)
        self.assertEqual(
            len([item for item in connection.bulk_executions if item[0].lstrip().startswith("INSERT")]),
            2,
        )

    def test_equal_precedence_conflict_fails_closed(self) -> None:
        source = observation()
        connection = FakeConnection()
        ingestor = PostgreSQLNextDayFcasIngestor(connection)
        ingestor.ingest((source,))
        changed_map = dict(connection.effective_rows[0][2].obj)
        changed_map["raise_6s"] = dict(changed_map["raise_6s"])
        changed_map["raise_6s"]["target_mw"] = 99.0
        conflicting_effective = list(connection.effective_rows[0])
        conflicting_effective[2] = Jsonb(changed_map)
        connection.effective_rows[0] = tuple(conflicting_effective)
        connection.bulk_executions = []
        connection.commits = connection.rollbacks = 0

        with self.assertRaisesRegex(NextDayFcasConflictError, "equal precedence"):
            ingestor.ingest((source,))

        self.assertEqual(connection.bulk_executions, [])
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))

    def test_conflicting_raw_replay_fails_closed(self) -> None:
        source = observation()
        connection = FakeConnection()
        ingestor = PostgreSQLNextDayFcasIngestor(connection)
        ingestor.ingest((source,))
        conflicting = list(connection.raw_rows[0])
        conflicting[3] = Jsonb({"raise_6s": {"target_mw": 99.0}})
        connection.raw_rows[0] = tuple(conflicting)
        connection.bulk_executions = []
        connection.commits = connection.rollbacks = 0

        with self.assertRaisesRegex(NextDayFcasConflictError, "stored raw"):
            ingestor.ingest((source,))

        self.assertEqual(connection.bulk_executions, [])
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))

    def test_newer_precedence_replaces_one_effective_group(self) -> None:
        source = observation()
        connection = FakeConnection()
        ingestor = PostgreSQLNextDayFcasIngestor(connection)
        ingestor.ingest((source,))
        changed_services = dict(source.fcas.services)
        changed_services["raise_6s"] = replace(
            source.fcas.raise_6s,
            target_mw=4.0,
        )
        newer = replace(
            source,
            correction_version=3,
            fcas=NextDayFcasObservation(changed_services),
        )

        result = ingestor.ingest((newer,))

        self.assertEqual(result.raw_inserted, 1)
        self.assertEqual(result.effective_candidates, 1)
        self.assertEqual(result.effective_applied, 1)
        self.assertEqual(result.effective_replayed, 0)
        self.assertEqual(len(connection.raw_rows), 2)
        self.assertEqual(len(connection.effective_rows), 1)
        self.assertEqual(connection.effective_rows[0][10], 3)
        self.assertEqual(
            connection.effective_rows[0][2].obj["raise_6s"]["target_mw"],
            4.0,
        )

    def test_requires_existing_nextday_soc_artifact_registration(self) -> None:
        connection = FakeConnection(artifact_row=("dispatch_scada",))

        with self.assertRaisesRegex(ValueError, "nextday_soc"):
            PostgreSQLNextDayFcasIngestor(connection).ingest((observation(),))

        self.assertEqual(connection.bulk_executions, [])
        self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
        self.assertFalse(
            any(
                "INSERT INTO historical_source_artifacts" in statement
                for statement, _ in connection.executions
            )
        )


if __name__ == "__main__":
    unittest.main()
