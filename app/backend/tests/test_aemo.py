"""Tests for the fixture-backed AEMO dispatch-price parser."""

from datetime import datetime, timedelta, timezone
import unittest

from batterywatch_api.aemo import (
    AemoParseError,
    parse_dispatch_price_csv,
    parse_dispatch_price_mms_csv,
)


UTC = timezone.utc
SOURCE_TIME = datetime(2026, 1, 1, 0, 6, tzinfo=UTC)
MMS_SOURCE_ID = "0000000000000001"
MMS_HEADER = (
    "I", "DISPATCH", "PRICE", "5", "SETTLEMENTDATE", "RUNNO", "REGIONID",
    "DISPATCHINTERVAL", "INTERVENTION", "RRP", "EEP", "ROP", "APCFLAG",
    "MARKETSUSPENDEDFLAG", "LASTCHANGED", "RAISE6SECRRP", "RAISE6SECROP",
    "RAISE6SECAPCFLAG", "RAISE60SECRRP", "RAISE60SECROP", "RAISE60SECAPCFLAG",
    "RAISE5MINRRP", "RAISE5MINROP", "RAISE5MINAPCFLAG", "RAISEREGRRP",
    "RAISEREGROP", "RAISEREGAPCFLAG", "LOWER6SECRRP", "LOWER6SECROP",
    "LOWER6SECAPCFLAG", "LOWER60SECRRP", "LOWER60SECROP", "LOWER60SECAPCFLAG",
    "LOWER5MINRRP", "LOWER5MINROP", "LOWER5MINAPCFLAG", "LOWERREGRRP",
    "LOWERREGROP", "LOWERREGAPCFLAG", "PRICE_STATUS", "PRE_AP_ENERGY_PRICE",
    "PRE_AP_RAISE6_PRICE", "PRE_AP_RAISE60_PRICE", "PRE_AP_RAISE5MIN_PRICE",
    "PRE_AP_RAISEREG_PRICE", "PRE_AP_LOWER6_PRICE", "PRE_AP_LOWER60_PRICE",
    "PRE_AP_LOWER5MIN_PRICE", "PRE_AP_LOWERREG_PRICE", "RAISE1SECRRP",
    "RAISE1SECROP", "RAISE1SECAPCFLAG", "LOWER1SECRRP", "LOWER1SECROP",
    "LOWER1SECAPCFLAG", "PRE_AP_RAISE1_PRICE", "PRE_AP_LOWER1_PRICE",
    "CUMUL_PRE_AP_ENERGY_PRICE", "CUMUL_PRE_AP_RAISE6_PRICE",
    "CUMUL_PRE_AP_RAISE60_PRICE", "CUMUL_PRE_AP_RAISE5MIN_PRICE",
    "CUMUL_PRE_AP_RAISEREG_PRICE", "CUMUL_PRE_AP_LOWER6_PRICE",
    "CUMUL_PRE_AP_LOWER60_PRICE", "CUMUL_PRE_AP_LOWER5MIN_PRICE",
    "CUMUL_PRE_AP_LOWERREG_PRICE", "CUMUL_PRE_AP_RAISE1_PRICE",
    "CUMUL_PRE_AP_LOWER1_PRICE", "OCD_STATUS", "MII_STATUS",
)


def _mms_row(
    region: str,
    rrp: str,
    *,
    interval: str = "2026/08/30 12:05:00",
    intervention: str = "0",
    apc_flag: str = "0",
    suspended: str = "0",
    price_status: str = "FIRM",
) -> str:
    values = [""] * len(MMS_HEADER)
    values[:4] = ["D", "DISPATCH", "PRICE", "5"]
    values[MMS_HEADER.index("SETTLEMENTDATE")] = interval
    values[MMS_HEADER.index("RUNNO")] = "1"
    values[MMS_HEADER.index("REGIONID")] = region
    values[MMS_HEADER.index("DISPATCHINTERVAL")] = "20260830097"
    values[MMS_HEADER.index("INTERVENTION")] = intervention
    values[MMS_HEADER.index("RRP")] = rrp
    values[MMS_HEADER.index("APCFLAG")] = apc_flag
    values[MMS_HEADER.index("MARKETSUSPENDEDFLAG")] = suspended
    values[MMS_HEADER.index("LASTCHANGED")] = "2026/08/30 12:00:11"
    values[MMS_HEADER.index("PRICE_STATUS")] = price_status
    return ",".join(values)


def _mms_payload(*rows: str, source_id: str = MMS_SOURCE_ID) -> str:
    report_rows = [
        f"C,NEMP.WORLD,DISPATCHIS,AEMO,PUBLIC,2026/08/30,12:05:15,{source_id},DISPATCHIS,0000000000000000",
        ",".join(MMS_HEADER),
        *rows,
    ]
    report_rows.append(f"C,END OF REPORT,{len(report_rows) + 1}")
    return "\n".join(report_rows) + "\n"


class AemoDispatchPriceParserTests(unittest.TestCase):
    def test_parses_complete_mms_price_batch_with_provenance_and_source_status(self):
        payload = _mms_payload(
            _mms_row("NSW1", "-4.93554", intervention="1", apc_flag="1"),
            _mms_row("QLD1", "-4.22661"),
            _mms_row("SA1", "-4.32012"),
            _mms_row("TAS1", "-4.94962"),
            _mms_row("VIC1", "-4.8574"),
        )

        records = parse_dispatch_price_mms_csv(
            payload,
            source_id=MMS_SOURCE_ID,
            ingestion_version=7,
            correction_version=2,
        )

        self.assertEqual(tuple(record.region for record in records), (
            "NSW1", "QLD1", "SA1", "TAS1", "VIC1",
        ))
        self.assertEqual(
            tuple(record.interval_start for record in records),
            (datetime(2026, 8, 30, 2, 5, tzinfo=UTC),) * 5,
        )
        self.assertEqual(records[0].price_aud_per_mwh, -4.93554)
        self.assertEqual(records[0].price_status, "negative")
        self.assertEqual(records[0].source_timestamp, datetime(2026, 8, 30, 2, 0, 11, tzinfo=UTC))
        self.assertEqual(records[0].quality_flags, (
            "runno=1", "intervention=1", "apcflag=1", "aemo_price_status=FIRM",
        ))
        self.assertEqual((records[0].intervention, records[0].apc_flag), (1, 1))
        self.assertFalse(records[0].market_suspended)

    def test_mms_price_parser_accepts_documented_non_firm_status(self):
        payload = _mms_payload(
            _mms_row("NSW1", "10", price_status="NOT FIRM"),
            _mms_row("QLD1", "11"),
            _mms_row("SA1", "12"),
            _mms_row("TAS1", "13"),
            _mms_row("VIC1", "14"),
        )

        records = parse_dispatch_price_mms_csv(
            payload,
            source_id=MMS_SOURCE_ID,
            ingestion_version=7,
        )

        self.assertIn("aemo_price_status=NOT FIRM", records[0].quality_flags)

    def test_mms_price_parser_rejects_incomplete_or_unsafe_reports(self):
        rows = [
            _mms_row("NSW1", "10"),
            _mms_row("QLD1", "11"),
            _mms_row("SA1", "12"),
            _mms_row("TAS1", "13"),
            _mms_row("VIC1", "14"),
        ]
        valid = _mms_payload(*rows)
        invalid_payloads = (
            valid.replace("C,END OF REPORT,8", "C,END OF REPORT,7"),
            valid.replace("I,DISPATCH,PRICE,5", "I,DISPATCH,PRICE,4", 1),
            valid.replace(",VIC1,20260830097", ",NSW1,20260830097", 1),
            valid.replace("2026/08/30 12:05:00", "2026/08/30 12:10:00", 1),
            valid.replace(",NSW1,20260830097", ",NSW2,20260830097", 1),
            valid.replace(",0,10,", ",-1,10,", 1),
            valid.replace(",10,", ",nan,", 1),
            valid.replace(",FIRM,", ",bad status,", 1),
            valid.replace("DISPATCHIS,AEMO", "DISPATCHIS\x00,AEMO", 1),
            _mms_payload(*rows[:-1]),
            _mms_payload(*rows, source_id="0000000000000002"),
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload[:50]):
                with self.assertRaises(AemoParseError):
                    parse_dispatch_price_mms_csv(
                        payload,
                        source_id=MMS_SOURCE_ID,
                        ingestion_version=7,
                    )

    def test_mms_parser_ignores_empty_fields_in_unrelated_tables(self) -> None:
        rows = _mms_payload(
            _mms_row("NSW1", "10"),
            _mms_row("QLD1", "11"),
            _mms_row("SA1", "12"),
            _mms_row("TAS1", "13"),
            _mms_row("VIC1", "14"),
        ).splitlines()
        rows.insert(-1, "D,DISPATCH,UNIT_SOLUTION,5,2026/08/30 12:05:00,,")
        rows[-1] = f"C,END OF REPORT,{len(rows)}"

        records = parse_dispatch_price_mms_csv(
            "\n".join(rows) + "\n",
            source_id=MMS_SOURCE_ID,
            ingestion_version=7,
        )

        self.assertEqual(len(records), 5)

    def test_parses_rows_into_typed_prices_with_provenance_flags(self):
        csv_text = """SETTLEMENTDATE,REGIONID,RRP,INTERVENTION,APCFLAG,RUNNO
2026/01/01 10:00:00,NSW1,125.50,0,0,1
2026/01/01 10:05:00,NSW1,-10.25,1,4,2
"""

        records = parse_dispatch_price_csv(
            csv_text,
            source_id="aemo-dispatch-fixture",
            source_timestamp=SOURCE_TIME,
            ingestion_version=7,
            correction_version=2,
            naive_timezone=timezone(timedelta(hours=10)),
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].interval_start, datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
        self.assertEqual(records[0].price_aud_per_mwh, 125.5)
        self.assertEqual(records[0].price_status, "available")
        self.assertEqual(records[0].provenance.source_id, "aemo-dispatch-fixture")
        self.assertEqual(records[0].provenance.ingestion_version, 7)
        self.assertEqual(records[0].provenance.correction_version, 2)
        self.assertIn("runno=1", records[0].quality_flags)
        self.assertIn("intervention=1", records[1].quality_flags)
        self.assertIn("apcflag=4", records[1].quality_flags)
        self.assertEqual(records[1].price_status, "negative")

    def test_maps_aemo_price_flags(self):
        csv_text = """SETTLEMENTDATE,REGIONID,RRP,INTERVENTION,APCFLAG,MARKETSUSPENDEDFLAG
2026/01/01 10:05:00,NSW1,125.50,1,4,1
"""

        records = parse_dispatch_price_csv(
            csv_text,
            source_id="aemo-dispatch-fixture",
            source_timestamp=SOURCE_TIME,
            ingestion_version=7,
            naive_timezone=timezone(timedelta(hours=10)),
        )

        actual = tuple(
            getattr(records[0], field, None)
            for field in ("intervention", "apc_flag", "market_suspended")
        )
        self.assertEqual(actual, (1, 4, True))

    def test_rejects_invalid_control_flags(self):
        cases = (
            ("INTERVENTION", "-1"),
            ("APCFLAG", "-1"),
            ("MARKETSUSPENDEDFLAG", "-1"),
            ("MARKETSUSPENDEDFLAG", "2"),
        )

        for field, value in cases:
            with self.subTest(field=field, value=value):
                values = {
                    "INTERVENTION": "0",
                    "APCFLAG": "0",
                    "MARKETSUSPENDEDFLAG": "0",
                }
                values[field] = value
                csv_text = f"""SETTLEMENTDATE,REGIONID,RRP,INTERVENTION,APCFLAG,MARKETSUSPENDEDFLAG
2026/01/01 10:05:00,NSW1,125.50,{values['INTERVENTION']},{values['APCFLAG']},{values['MARKETSUSPENDEDFLAG']}
"""

                with self.assertRaises(AemoParseError):
                    parse_dispatch_price_csv(
                        csv_text,
                        source_id="aemo-dispatch-fixture",
                        source_timestamp=SOURCE_TIME,
                        ingestion_version=7,
                        naive_timezone=timezone(timedelta(hours=10)),
                    )

    def test_blank_rrp_is_missing_sorted_rows(self):
        csv_text = """SETTLEMENTDATE,REGIONID,RRP,INTERVENTION,APCFLAG,RUNNO
2026-01-01T10:10:00+10:00,NSW1,,0,0,3
2026-01-01T10:00:00+10:00,NSW1,0,0,0,1
"""

        records = parse_dispatch_price_csv(
            csv_text,
            source_id="aemo-dispatch-fixture",
            source_timestamp=SOURCE_TIME,
            ingestion_version=1,
        )

        self.assertEqual([record.interval_start for record in records], [
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
        ])
        self.assertEqual(records[0].price_aud_per_mwh, 0.0)
        self.assertIsNone(records[1].price_aud_per_mwh)
        self.assertEqual(records[1].price_status, "missing")

    def test_malformed_or_inconsistent_rows_fail_closed(self):
        cases = (
            "REGIONID,RRP\nNSW1,10\n",
            "SETTLEMENTDATE,REGIONID,RRP\nnot-a-time,NSW1,10\n",
            "SETTLEMENTDATE,REGIONID,RRP\n2026/01/01 10:01:00,NSW1,10\n",
            "SETTLEMENTDATE,REGIONID,RRP\n2026/01/01 10:00:00,,10\n",
        )

        for csv_text in cases:
            with self.subTest(csv_text=csv_text):
                with self.assertRaises(AemoParseError):
                    parse_dispatch_price_csv(
                        csv_text,
                        source_id="aemo-dispatch-fixture",
                        source_timestamp=SOURCE_TIME,
                        ingestion_version=1,
                    )


if __name__ == "__main__":
    unittest.main()
