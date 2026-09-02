"""Tests for the strict canonical Dispatch SCADA parser."""

from datetime import datetime, timedelta, timezone
from typing import Any
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from batterywatch_api.dispatch_scada import (
    DispatchScadaParseError,
    parse_dispatch_scada_csv,
)
from batterywatch_api.storage import GeneratorPower5m


_SOURCE_ARTIFACT_ID = "0000000535047618"
_NAIVE_TIMEZONE = timezone(timedelta(hours=10))
_CANONICAL_PAYLOAD = """C,NEMP.WORLD,DISPATCHSCADA,AEMO,PUBLIC,2026/08/29,14:10:15,0000000535047618,DISPATCHSCADA,0000000535047612
I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE,LASTCHANGED
D,DISPATCH,UNIT_SCADA,1,"2026/08/29 14:10:00",BWTR2,0,"2026/08/29 14:10:11"
D,DISPATCH,UNIT_SCADA,1,"2026/08/29 14:05:00",BWTR1,-12.460880,"2026/08/29 14:05:11"
C,END OF REPORT,5
"""


class DispatchScadaParserTests(unittest.TestCase):
    def _parse(
        self,
        payload: str = _CANONICAL_PAYLOAD,
        *,
        naive_timezone: Any = _NAIVE_TIMEZONE,
    ):
        return parse_dispatch_scada_csv(
            payload,
            source_artifact_id=_SOURCE_ARTIFACT_ID,
            ingestion_version=7,
            correction_version=2,
            naive_timezone=naive_timezone,
        )

    def test_rejects_offset_free_timestamp_without_timezone(self):
        with self.assertRaises(DispatchScadaParseError):
            self._parse(naive_timezone=None)

    def test_rejects_wrong_metadata_discriminator(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "C,NEMP.WORLD,DISPATCHSCADA,AEMO,PUBLIC",
            "X,NEMP.WORLD,DISPATCHSCADA,AEMO,PUBLIC",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_metadata_source(self):
        payload = _CANONICAL_PAYLOAD.replace("NEMP.WORLD", "OTHER.SOURCE", 1)

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_metadata_table(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "NEMP.WORLD,DISPATCHSCADA,AEMO",
            "NEMP.WORLD,OTHER_TABLE,AEMO",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_metadata_source_sequence_mismatch(self):
        payload = _CANONICAL_PAYLOAD.replace(
            _SOURCE_ARTIFACT_ID,
            "0000000535047619",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_metadata_report_table(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "DISPATCHSCADA,0000000535047612",
            "OTHER_TABLE,0000000535047612",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_header_discriminator(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE",
            "X,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_header_source(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "I,DISPATCH,UNIT_SCADA",
            "I,OTHER,UNIT_SCADA",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_header_table(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "I,DISPATCH,UNIT_SCADA",
            "I,DISPATCH,OTHER_TABLE",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_header_version(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE",
            "I,DISPATCH,UNIT_SCADA,2,SETTLEMENTDATE",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_header_columns(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "SETTLEMENTDATE,DUID,SCADAVALUE,LASTCHANGED",
            "SETTLEMENTDATE,BAD_DUID,SCADAVALUE,LASTCHANGED",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_metadata_row_length(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "DISPATCHSCADA,0000000535047612\nI,",
            "DISPATCHSCADA\nI,",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_header_row_length(self):
        payload = _CANONICAL_PAYLOAD.replace(
            ",SCADAVALUE,LASTCHANGED\nD,",
            ",SCADAVALUE\nD,",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_metadata_publisher(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "DISPATCHSCADA,AEMO,PUBLIC",
            "DISPATCHSCADA,OTHER,PUBLIC",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_metadata_visibility(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "DISPATCHSCADA,AEMO,PUBLIC",
            "DISPATCHSCADA,AEMO,PRIVATE",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_data_discriminator(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "D,DISPATCH,UNIT_SCADA,1",
            "X,DISPATCH,UNIT_SCADA,1",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_data_source(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "D,DISPATCH,UNIT_SCADA,1",
            "D,OTHER,UNIT_SCADA,1",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_data_table(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "D,DISPATCH,UNIT_SCADA,1",
            "D,DISPATCH,OTHER_TABLE,1",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_data_version(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "D,DISPATCH,UNIT_SCADA,1",
            "D,DISPATCH,UNIT_SCADA,2",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_wrong_data_row_length(self):
        payload = _CANONICAL_PAYLOAD.replace(
            '"2026/08/29 14:10:11"\nD,',
            '"2026/08/29 14:10:11",EXTRA\nD,',
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_non_numeric_power(self):
        payload = _CANONICAL_PAYLOAD.replace("-12.460880", "not-a-number", 1)

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_non_finite_power(self):
        payload = _CANONICAL_PAYLOAD.replace("-12.460880", "nan", 1)

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_invalid_naive_timezone(self):
        with self.assertRaises(DispatchScadaParseError):
            self._parse(naive_timezone="not-a-timezone")

    def test_rejects_dst_aware_naive_timezone(self):
        with self.assertRaises(DispatchScadaParseError):
            self._parse(naive_timezone=ZoneInfo("Australia/Sydney"))

    def test_rejects_wrong_fixed_naive_timezone(self):
        with self.assertRaises(DispatchScadaParseError):
            self._parse(naive_timezone=timezone(timedelta(hours=11)))

    def test_rejects_bad_timestamp_timezone_offset(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "2026/08/29 14:10:00",
            "2026/08/29 14:10:00+99:00",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_invalid_interval_timestamp(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "2026/08/29 14:10:00",
            "not-a-timestamp",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_misaligned_interval_timestamp(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "2026/08/29 14:10:00",
            "2026/08/29 14:06:00",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_invalid_source_timestamp(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "2026/08/29 14:10:11",
            "not-a-timestamp",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_blank_duid(self):
        payload = _CANONICAL_PAYLOAD.replace(
            ",BWTR1,-12.460880",
            ",,-12.460880",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_infinite_power(self):
        payload = _CANONICAL_PAYLOAD.replace("-12.460880", "inf", 1)

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_missing_footer(self):
        payload = _CANONICAL_PAYLOAD.replace("C,END OF REPORT,5\n", "", 1)

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_footer_with_extra_field(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "C,END OF REPORT,5",
            "C,END OF REPORT,5,EXTRA",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_footer_with_missing_field(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "C,END OF REPORT,5",
            "C,END OF REPORT",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_footer_count_mismatch(self):
        payload = _CANONICAL_PAYLOAD.replace("C,END OF REPORT,5", "C,END OF REPORT,4", 1)

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_non_numeric_footer_count(self):
        payload = _CANONICAL_PAYLOAD.replace("C,END OF REPORT,5", "C,END OF REPORT,total", 1)

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_invalid_footer_marker(self):
        payload = _CANONICAL_PAYLOAD.replace(
            "C,END OF REPORT,5",
            "C,END OF DATA,5",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_member_with_no_data_rows(self):
        payload = "\n".join(
            _CANONICAL_PAYLOAD.splitlines()[:2] + ["C,END OF REPORT,3"]
        ) + "\n"

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_metadata_only_envelope(self):
        payload = _CANONICAL_PAYLOAD.splitlines()[0] + "\n"

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_metadata_and_header_only_envelope(self):
        payload = "\n".join(_CANONICAL_PAYLOAD.splitlines()[:2]) + "\n"

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_duplicate_duid_and_interval(self):
        payload = _CANONICAL_PAYLOAD.replace("BWTR2", "BWTR1", 1)
        payload = payload.replace(
            "2026/08/29 14:05:00",
            "2026/08/29 14:10:00",
            1,
        )

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_empty_payload_with_public_error(self):
        with self.assertRaises(DispatchScadaParseError):
            self._parse("")

    def test_rejects_non_string_payload_with_public_error(self):
        with self.assertRaises(DispatchScadaParseError):
            self._parse(Mock(spec=str))

    def test_rejects_malformed_csv(self):
        payload = _CANONICAL_PAYLOAD + 'D,"unterminated\n'

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_rejects_invalid_metadata_timestamp(self):
        payload = _CANONICAL_PAYLOAD.replace("2026/08/29", "not-a-date", 1)

        with self.assertRaises(DispatchScadaParseError):
            self._parse(payload)

    def test_parses_complete_member_deterministically(self):
        actual = self._parse()

        self.assertEqual(
            actual,
            (
                GeneratorPower5m(
                    generator_id="BWTR1",
                    interval_start=datetime(2026, 8, 29, 4, 5, tzinfo=timezone.utc),
                    power_mw=-12.460880,
                    source_id="0000000535047618",
                    source_timestamp=datetime(2026, 8, 29, 4, 5, 11, tzinfo=timezone.utc),
                    ingestion_version=7,
                    correction_version=2,
                ),
                GeneratorPower5m(
                    generator_id="BWTR2",
                    interval_start=datetime(2026, 8, 29, 4, 10, tzinfo=timezone.utc),
                    power_mw=0.0,
                    source_id="0000000535047618",
                    source_timestamp=datetime(2026, 8, 29, 4, 10, 11, tzinfo=timezone.utc),
                    ingestion_version=7,
                    correction_version=2,
                ),
            ),
        )
