"""Tests for regional aggregate BDU energy-storage parsing."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import unittest

from batterywatch_api.regional_bdu_soc import (
    RegionalBduSocParseError,
    parse_dispatch_regionsum_bdu_soc,
)

UTC = timezone.utc
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "historical"
    / "dispatch-regionsum-bdu-soc-20260830-2145-reduced.csv"
)


class RegionalBduSocParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = FIXTURE.read_text(encoding="utf-8")
        self.downloaded_at = datetime(2026, 8, 30, 11, 45, tzinfo=UTC)

    def parse(self, payload: str | None = None):
        return parse_dispatch_regionsum_bdu_soc(
            self.payload if payload is None else payload,
            source_artifact_id=(
                "e83dd01b41bd3a5eef355e16d72d79f176a652ee27471615971c55a4c4f42561"
            ),
            downloaded_at=self.downloaded_at,
            ingestion_version=8,
            correction_version=2,
        )

    def test_parses_real_derived_regional_values_and_null(self) -> None:
        observations = self.parse()

        self.assertEqual(len(observations), 5)
        self.assertEqual(
            [item.region_id for item in observations],
            ["NSW1", "QLD1", "SA1", "TAS1", "VIC1"],
        )
        nsw = observations[0]
        self.assertEqual(nsw.interval_start, datetime(2026, 8, 30, 11, 45, tzinfo=UTC))
        self.assertEqual(nsw.soc_mwh, 2343.89364)
        self.assertEqual(nsw.intervention, 0)
        self.assertEqual(nsw.run_number, 1)
        self.assertEqual(nsw.dispatch_interval, "20260830213")
        self.assertEqual(nsw.last_changed, datetime(2026, 8, 30, 11, 40, 4, tzinfo=UTC))
        self.assertEqual(
            nsw.report_timestamp,
            datetime(2026, 8, 30, 11, 40, 9, tzinfo=UTC),
        )
        self.assertEqual(nsw.downloaded_at, self.downloaded_at)
        self.assertEqual(nsw.scope, "regional_aggregate")
        self.assertEqual(nsw.publication_status, "near_real_time_regional")
        self.assertEqual(nsw.ingestion_version, 8)
        self.assertEqual(nsw.correction_version, 2)
        tas = next(item for item in observations if item.region_id == "TAS1")
        self.assertIsNone(tas.soc_mwh)

    def test_never_exposes_regional_aggregate_as_duid_data(self) -> None:
        observations = self.parse()

        self.assertTrue(all(not hasattr(item, "duid") for item in observations))
        self.assertEqual(
            {item.region_id for item in observations},
            {"NSW1", "QLD1", "SA1", "TAS1", "VIC1"},
        )

    def test_rejects_wrong_version_duplicate_rows_and_bad_trailer_count(self) -> None:
        data_row = self.payload.splitlines()[2]
        duplicate = self.payload.replace(
            'C,"END OF REPORT",8',
            data_row + '\nC,"END OF REPORT",9',
        )
        missing_region = "\n".join(
            line for line in self.payload.splitlines() if ",TAS1," not in line
        ).replace('C,"END OF REPORT",8', 'C,"END OF REPORT",7')
        cases = (
            self.payload.replace("I,DISPATCH,REGIONSUM,9,", "I,DISPATCH,REGIONSUM,8,", 1),
            duplicate,
            missing_region,
            self.payload.replace('C,"END OF REPORT",8', 'C,"END OF REPORT",9'),
        )
        for payload in cases:
            with self.subTest(payload=payload[-100:]):
                with self.assertRaises(RegionalBduSocParseError):
                    self.parse(payload)

    def test_rejects_invalid_region_negative_nonfinite_and_misaligned_values(self) -> None:
        cases = (
            self.payload.replace(",NSW1,", ",XX1,", 1),
            self.payload.replace(",2343.893640\n", ",-1\n", 1),
            self.payload.replace(",2343.893640\n", ",NaN\n", 1),
            self.payload.replace("2026/08/30 21:45:00", "2026/08/30 21:46:00", 1),
        )
        for payload in cases:
            with self.subTest(payload=payload[:100]):
                with self.assertRaises(RegionalBduSocParseError):
                    self.parse(payload)

    def test_rejects_invalid_provenance_inputs_before_parsing(self) -> None:
        valid: dict[str, Any] = {
            "payload": self.payload,
            "source_artifact_id": (
                "e83dd01b41bd3a5eef355e16d72d79f176a652ee27471615971c55a4c4f42561"
            ),
            "downloaded_at": self.downloaded_at,
            "ingestion_version": 8,
            "correction_version": 2,
        }
        invalid: tuple[dict[str, Any], ...] = (
            {"payload": b"not text"},
            {"source_artifact_id": "not-a-digest"},
            {"downloaded_at": datetime(2026, 8, 30)},
            {"ingestion_version": True},
            {"correction_version": -1},
        )
        for override in invalid:
            with self.subTest(override=override):
                arguments = valid | override
                with self.assertRaises(RegionalBduSocParseError):
                    parse_dispatch_regionsum_bdu_soc(**arguments)


if __name__ == "__main__":
    unittest.main()
