from datetime import datetime, timedelta, timezone
import unittest

from batterywatch_api.dispatch_scada_ingestion import (
    DispatchScadaArtifactReceipt,
    DispatchScadaConflictError,
    DispatchScadaIngestionResult,
    PostgreSQLDispatchScadaIngestor,
    RawDispatchScadaObservation,
)
from batterywatch_api.storage import GeneratorMetadata, GeneratorPower5m


UTC = timezone.utc
REPORT_TIMESTAMP = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
INTERVAL_ONE = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
INTERVAL_TWO = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
SOURCE_TIMESTAMP = datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters):
        self.connection.executions.append((statement, tuple(parameters)))
        if len(self.connection.executions) == self.connection.fail_on_execute:
            raise self.connection.failure

    def fetchone(self):
        if self.connection.fetchone_results:
            return self.connection.fetchone_results.pop(0)
        return (1,)

    def close(self):
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(self, *, fail_on_execute=None, failure=None, fetchone_results=None):
        self.executions = []
        self.fail_on_execute = fail_on_execute
        self.failure = failure
        self.fetchone_results = list(fetchone_results or ())
        self.cursor_calls = self.closed_cursors = 0
        self.commits = self.rollbacks = 0
        self._cursor = FakeCursor(self)

    def cursor(self):
        self.cursor_calls += 1
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def receipt():
    filename = "PUBLIC_DISPATCHSCADA_202601010000_123.zip"
    return DispatchScadaArtifactReceipt(
        "123",
        "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/" + filename,
        filename,
        filename.removesuffix(".zip") + ".CSV",
        REPORT_TIMESTAMP,
        "a" * 64,
        b"zip-payload",
    )


def observations():
    return (
        RawDispatchScadaObservation(
            "123", "BAT-1", INTERVAL_ONE, 1.25, SOURCE_TIMESTAMP
        ),
        RawDispatchScadaObservation(
            "123", "BAT-2", INTERVAL_TWO, 0.0, SOURCE_TIMESTAMP, 2, 1
        ),
    )


class PostgreSQLDispatchScadaIngestorReplayTests(unittest.TestCase):
    def test_exact_replay_is_a_clean_noop(self):
        artifact = receipt()
        stored = (
            artifact.source_artifact_id,
            artifact.source_url,
            artifact.zip_filename,
            artifact.csv_member_name,
            artifact.report_timestamp,
            artifact.zip_sha256,
            memoryview(artifact.raw_zip),
        )
        connection = FakeConnection(fetchone_results=[None, stored])

        result = PostgreSQLDispatchScadaIngestor(connection).ingest(
            artifact, observations()
        )

        self.assertEqual(
            (result, len(connection.executions), connection.cursor_calls,
             connection.closed_cursors, connection.commits, connection.rollbacks),
            (DispatchScadaIngestionResult(0, 0, True), 2, 1, 1, 1, 0),
        )
        self.assertIn("ON CONFLICT DO NOTHING RETURNING 1", connection.executions[0][0])
        self.assertIn("SELECT", connection.executions[1][0])

    def test_alternate_official_source_url_replay_is_a_clean_noop(self):
        artifact = receipt()
        stored = (
            artifact.source_artifact_id,
            (
                "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/"
                "PUBLIC_DISPATCHSCADA_20260101.zip#"
                + artifact.zip_filename
            ),
            artifact.zip_filename,
            artifact.csv_member_name,
            artifact.report_timestamp,
            artifact.zip_sha256,
            memoryview(artifact.raw_zip),
        )
        connection = FakeConnection(fetchone_results=[None, stored])

        result = PostgreSQLDispatchScadaIngestor(connection).ingest(
            artifact, observations()
        )

        self.assertEqual(result, DispatchScadaIngestionResult(0, 0, True))
        self.assertEqual((connection.commits, connection.rollbacks), (1, 0))


class PostgreSQLDispatchScadaIngestorConflictTests(unittest.TestCase):
    def test_immutable_conflict_rolls_back_once(self):
        artifact = receipt()
        stored = (
            artifact.source_artifact_id,
            artifact.source_url,
            artifact.zip_filename,
            artifact.csv_member_name,
            artifact.report_timestamp,
            "b" * 64,
            artifact.raw_zip,
        )
        connection = FakeConnection(fetchone_results=[None, stored])

        with self.assertRaises(DispatchScadaConflictError):
            PostgreSQLDispatchScadaIngestor(connection).ingest(artifact, observations())

        self.assertEqual(
            (len(connection.executions), connection.executions[1][1],
             connection.cursor_calls, connection.closed_cursors,
             connection.commits, connection.rollbacks),
            (2, (artifact.source_artifact_id,), 1, 1, 0, 1),
        )


class PostgreSQLDispatchScadaIngestorMappingTests(unittest.TestCase):
    def test_persists_raw_rows_then_guarded_mapping_in_one_transaction(self):
        artifact = receipt()
        generator = GeneratorMetadata(
            "BAT-1", "Site One", "NSW", 10, 20, "registry", SOURCE_TIMESTAMP, 3, 2
        )
        power = GeneratorPower5m(
            "BAT-1", INTERVAL_ONE, 1.25, "dispatch", SOURCE_TIMESTAMP, 4, 1
        )
        connection = FakeConnection()

        result = PostgreSQLDispatchScadaIngestor(connection).ingest(
            artifact,
            observations(),
            generators=(generator,),
            power_records=(power,),
        )

        self.assertEqual(
            tuple(
                (
                    "artifact" if "dispatch_scada_artifacts" in statement
                    else "observation" if "raw_dispatch_scada_observations" in statement
                    else "generator" if "INSERT INTO generators" in statement
                    else "bounds" if "UPDATE generators" in statement
                    else "power",
                    parameters,
                )
                for statement, parameters in connection.executions
            ),
            (
                ("artifact", ("123", artifact.source_url, artifact.zip_filename,
                               artifact.csv_member_name, REPORT_TIMESTAMP, "a" * 64,
                               b"zip-payload")),
                ("observation", ("123", "BAT-1", INTERVAL_ONE, 1.25,
                                  SOURCE_TIMESTAMP, 0, 0)),
                ("observation", ("123", "BAT-2", INTERVAL_TWO, 0.0,
                                  SOURCE_TIMESTAMP, 2, 1)),
                ("generator", ("BAT-1", "Site One", "NSW", 10.0, 20.0,
                                None, None,
                                "registry", SOURCE_TIMESTAMP, 3, 2)),
                ("power", ("BAT-1", INTERVAL_ONE, 1.25, "dispatch",
                            SOURCE_TIMESTAMP, 4, 1)),
                ("bounds", (INTERVAL_ONE, INTERVAL_ONE + timedelta(minutes=5),
                            "BAT-1")),
            ),
        )
        self.assertEqual(
            (result, connection.cursor_calls, connection.closed_cursors,
             connection.commits, connection.rollbacks),
            (DispatchScadaIngestionResult(2, 1, False), 1, 1, 1, 0),
        )
        self.assertTrue(all("%s" in statement for statement, _ in connection.executions))
        self.assertTrue(all(
            "ON CONFLICT" in statement
            for statement, _ in (connection.executions[0], connection.executions[3],
                                 connection.executions[4])
        ))
        self.assertIn("LEAST", connection.executions[5][0])
        self.assertIn("GREATEST", connection.executions[5][0])


class PostgreSQLDispatchScadaIngestorMappedFailureTests(unittest.TestCase):
    def test_mapped_write_failure_rolls_back_once_and_reraises_same_error(self):
        failure = RuntimeError("mapped power write failed")
        generator = GeneratorMetadata(
            "BAT-1", "Site One", "NSW", 10, 20, "registry", SOURCE_TIMESTAMP, 3, 2
        )
        power = GeneratorPower5m(
            "BAT-1", INTERVAL_ONE, 1.25, "dispatch", SOURCE_TIMESTAMP, 4, 1
        )
        connection = FakeConnection(fail_on_execute=5, failure=failure)
        same_error = False

        try:
            PostgreSQLDispatchScadaIngestor(connection).ingest(
                receipt(),
                observations(),
                generators=(generator,),
                power_records=(power,),
            )
        except RuntimeError as raised:
            same_error = raised is failure
        else:
            self.fail("expected the mapped power write failure")

        self.assertEqual(
            (same_error, connection.commits, connection.rollbacks,
             connection.cursor_calls, connection.closed_cursors),
            (True, 0, 1, 1, 1),
        )


class PostgreSQLDispatchScadaIngestorSuccessTests(unittest.TestCase):
    def test_inserts_artifact_then_two_observations_in_one_commit(self):
        artifact = receipt()
        batch = observations()
        connection = FakeConnection()

        accepted = PostgreSQLDispatchScadaIngestor(connection).ingest(artifact, batch)
        executions = tuple(
            (
                "artifact" if "dispatch_scada_artifacts" in statement else "observation",
                parameters,
                "%s" in statement,
                "ON CONFLICT" not in statement,
            )
            for statement, parameters in connection.executions
        )
        self.assertEqual(
            (accepted, executions, connection.cursor_calls, connection.closed_cursors,
             connection.commits, connection.rollbacks),
            (
                DispatchScadaIngestionResult(2, 0, False),
                (
                    ("artifact", ("123", artifact.source_url, artifact.zip_filename,
                                   artifact.csv_member_name, REPORT_TIMESTAMP, "a" * 64,
                                   b"zip-payload"), True, False),
                    ("observation", ("123", "BAT-1", INTERVAL_ONE, 1.25,
                                      SOURCE_TIMESTAMP, 0, 0), True, True),
                    ("observation", ("123", "BAT-2", INTERVAL_TWO, 0.0,
                                      SOURCE_TIMESTAMP, 2, 1), True, True),
                ),
                1, 1, 1, 0,
            ),
        )


class PostgreSQLDispatchScadaIngestorFailureTests(unittest.TestCase):
    def test_mid_batch_execute_failure_rolls_back_once_and_reraises_same_error(self):
        failure = RuntimeError("mid-batch execute failed")
        connection = FakeConnection(fail_on_execute=3, failure=failure)
        same_error = False

        try:
            PostgreSQLDispatchScadaIngestor(connection).ingest(receipt(), observations())
        except RuntimeError as raised:
            same_error = raised is failure
        else:
            self.fail("expected the mid-batch execute failure")

        self.assertEqual(
            (same_error, connection.commits, connection.rollbacks,
             connection.cursor_calls, connection.closed_cursors),
            (True, 0, 1, 1, 1),
        )


if __name__ == "__main__":
    unittest.main()
