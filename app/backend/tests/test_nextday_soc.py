"""Tests for authoritative Next Day UnitSolution SOC parsing."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import unittest

from batterywatch_api.nextday_soc import NextDaySocParseError, parse_nextday_unit_solution_soc

UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "historical" / "nextday-unit-solution-soc-20260829-reduced.csv"


class NextDaySocParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = FIXTURE.read_text(encoding="utf-8")
        self.downloaded_at = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)

    def parse(self, payload: str | None = None, *, duids: frozenset[str] = frozenset(("ADPBA1", "KEPBG1"))):
        return parse_nextday_unit_solution_soc(
            self.payload if payload is None else payload,
            duids=duids,
            source_artifact_id="d7a2abdd2947ed4b222166b9f60e3a8052838190027dd9ce03cb291ba2d29bc4",
            downloaded_at=self.downloaded_at,
            ingestion_version=8,
            correction_version=2,
        )

    def test_parses_real_derived_mwh_null_and_publication_latency(self) -> None:
        observations = self.parse()

        self.assertEqual(len(observations), 3)
        first = observations[0]
        self.assertEqual(first.duid, "ADPBA1")
        self.assertEqual(first.interval_start, datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        self.assertEqual(first.soc_mwh, 3.786)
        self.assertEqual(first.intervention, 0)
        self.assertEqual(first.run_number, 1)
        self.assertEqual(first.dispatch_interval, "20260829001")
        self.assertEqual(first.last_changed, datetime(2026, 8, 28, 18, 0, 6, tzinfo=UTC))
        self.assertEqual(first.report_timestamp, datetime(2026, 8, 29, 18, 10, tzinfo=UTC))
        self.assertEqual(first.downloaded_at, self.downloaded_at)
        self.assertEqual(first.publication_latency_seconds, 86_700)
        self.assertEqual(first.publication_status, "next_day")
        self.assertEqual(first.ingestion_version, 8)
        self.assertEqual(first.correction_version, 2)
        missing = next(item for item in observations if item.duid == "KEPBG1")
        self.assertIsNone(missing.soc_mwh)
        self.assertEqual(
            [(item.duid, item.interval_start) for item in observations],
            sorted((item.duid, item.interval_start) for item in observations),
        )

    def test_filters_to_reviewed_duids_without_inventing_missing_rows(self) -> None:
        observations = self.parse(duids=frozenset(("ADPBA1",)))
        self.assertEqual(len(observations), 2)
        self.assertEqual({item.duid for item in observations}, {"ADPBA1"})

    def test_rejects_wrong_version_duplicate_rows_and_bad_trailer_count(self) -> None:
        duplicate = self.payload.replace(
            'C,"END OF REPORT",6',
            self.payload.splitlines()[2] + '\nC,"END OF REPORT",7',
        )
        cases = (
            self.payload.replace("I,DISPATCH,UNIT_SOLUTION,6,", "I,DISPATCH,UNIT_SOLUTION,5,", 1),
            duplicate,
            self.payload.replace('C,"END OF REPORT",6', 'C,"END OF REPORT",7'),
        )
        for payload in cases:
            with self.subTest(payload=payload[-80:]):
                with self.assertRaises(NextDaySocParseError):
                    self.parse(payload)

    def test_rejects_negative_nonfinite_or_misaligned_authoritative_values(self) -> None:
        cases = (
            self.payload.replace(",3.786,3.78315,", ",-1,3.78315,", 1),
            self.payload.replace(",3.786,3.78315,", ",NaN,3.78315,", 1),
            self.payload.replace("2026/08/29 04:05:00", "2026/08/29 04:06:00", 1),
        )
        for payload in cases:
            with self.subTest(payload=payload[0:80]):
                with self.assertRaises(NextDaySocParseError):
                    self.parse(payload)

    def test_rejects_invalid_provenance_inputs_before_parsing(self) -> None:
        valid = {
            "payload": self.payload,
            "duids": frozenset(("ADPBA1",)),
            "source_artifact_id": "d7a2abdd2947ed4b222166b9f60e3a8052838190027dd9ce03cb291ba2d29bc4",
            "downloaded_at": self.downloaded_at,
            "ingestion_version": 8,
            "correction_version": 2,
        }
        invalid: tuple[dict[str, Any], ...] = (
            {"duids": {"ADPBA1"}},
            {"source_artifact_id": "not-a-digest"},
            {"downloaded_at": datetime(2026, 8, 30)},
            {"ingestion_version": True},
            {"correction_version": -1},
        )
        for override in invalid:
            with self.subTest(override=override):
                arguments = valid | override
                with self.assertRaises(NextDaySocParseError):
                    parse_nextday_unit_solution_soc(**arguments)


if __name__ == "__main__":
    unittest.main()
