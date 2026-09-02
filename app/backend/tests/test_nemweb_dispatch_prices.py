"""Tests for the official NEMWeb DispatchIS regional-price adapter."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import batterywatch_api.nemweb_dispatch_prices as source
from batterywatch_api.nemweb_http import NemwebHttpResource
from batterywatch_api.storage import RegionalPrice5m


INDEX_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
FILENAME = "PUBLIC_DISPATCHIS_202608301205_0000000535164870.zip"
ARTIFACT_URL = INDEX_URL + FILENAME
SOURCE_ID = "0000000535164870"


def _resource(url: str, body: bytes) -> NemwebHttpResource:
    return NemwebHttpResource(
        requested_url=url,
        resolved_url=url,
        body=body,
        content_type=None,
        etag=None,
        last_modified=None,
    )


def _zip(payload: str, *, member_name: str | None = None) -> tuple[bytes, str]:
    expected = FILENAME.removesuffix(".zip") + ".CSV"
    member = member_name or expected
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(member, payload.encode("utf-8"))
    return buffer.getvalue(), expected


class NemwebDispatchPriceAdapterTests(unittest.TestCase):
    def test_discovers_canonical_artifacts_and_extracts_verified_member(self) -> None:
        index_html = f"""
        <a href="PUBLIC_DISPATCHIS_202608301200_0000000535164869.zip">older</a>
        <a href="{FILENAME}">newer</a>
        <a href="{FILENAME}">duplicate</a>
        <a href="https://example.invalid/{FILENAME}">external</a>
        """
        references = source.discover_dispatch_price_artifacts(
            index_html,
            index_url=INDEX_URL,
        )
        self.assertEqual(tuple(item.source_artifact_id for item in references), (
            "0000000535164869", SOURCE_ID,
        ))
        reference = references[-1]
        self.assertEqual(reference.report_timestamp, datetime(2026, 8, 30, 2, 5, tzinfo=timezone.utc))
        with self.assertRaises(FrozenInstanceError):
            reference.url = "changed"  # type: ignore[misc]

        csv_payload = "C,NEMP.WORLD,DISPATCHIS,AEMO,PUBLIC\nC,END OF REPORT,2\n"
        zip_payload, member_name = _zip(csv_payload)
        artifact = source.extract_dispatch_price_zip(reference, zip_payload)
        self.assertEqual(
            (
                artifact.csv_member_name,
                artifact.csv_payload,
                artifact.zip_sha256,
                artifact.raw_zip,
            ),
            (
                member_name,
                csv_payload,
                sha256(zip_payload).hexdigest(),
                zip_payload,
            ),
        )

    def test_collects_latest_artifact_and_delegates_strict_parser(self) -> None:
        index_html = f'<a href="{FILENAME}">latest</a>'
        csv_payload = "C,NEMP.WORLD,DISPATCHIS,AEMO,PUBLIC\nC,END OF REPORT,2\n"
        zip_payload, _member_name = _zip(csv_payload)
        calls: list[tuple[str, int]] = []

        def fetch(url: str, *, max_bytes: int) -> NemwebHttpResource:
            calls.append((url, max_bytes))
            body = index_html.encode("utf-8") if url == INDEX_URL else zip_payload
            return _resource(url, body)

        expected_records = (
            RegionalPrice5m(
                region="NSW1",
                interval_start=datetime(2026, 8, 30, 2, 5, tzinfo=timezone.utc),
                price_aud_per_mwh=-4.9,
                price_status="negative",
                source_id=SOURCE_ID,
                source_timestamp=datetime(2026, 8, 30, 2, 0, 11, tzinfo=timezone.utc),
                ingestion_version=7,
                correction_version=2,
            ),
        )
        with patch.object(
            source,
            "parse_dispatch_price_mms_csv",
            return_value=expected_records,
        ) as parser:
            result = source.collect_latest_dispatch_prices(
                ingestion_version=7,
                correction_version=2,
                fetch=fetch,
            )

        self.assertEqual(result.records, expected_records)
        self.assertEqual(result.artifact.reference.source_artifact_id, SOURCE_ID)
        self.assertEqual(calls, [
            (INDEX_URL, source.DISPATCH_PRICE_INDEX_MAX_BYTES),
            (ARTIFACT_URL, source.DISPATCH_PRICE_ARTIFACT_MAX_BYTES),
        ])
        parser.assert_called_once_with(
            csv_payload,
            source_id=SOURCE_ID,
            ingestion_version=7,
            correction_version=2,
        )

    def test_rejects_noncanonical_or_unsafe_source_inputs(self) -> None:
        invalid_indexes = (
            "",
            '<a href="nested/' + FILENAME + '">nested</a>',
            '<a href="PUBLIC_DISPATCHIS_202602301205_1.zip">bad date</a>',
        )
        for index_html in invalid_indexes:
            with self.subTest(index_html=index_html):
                with self.assertRaises(source.NemwebDispatchPriceError):
                    source.discover_dispatch_price_artifacts(
                        index_html,
                        index_url=INDEX_URL,
                    )

        reference = source.DispatchPriceArtifactRef(
            url=ARTIFACT_URL,
            zip_filename=FILENAME,
            source_artifact_id=SOURCE_ID,
            report_timestamp=datetime(2026, 8, 30, 2, 5, tzinfo=timezone.utc),
        )
        bad_archives = (
            b"not a zip",
            _zip("valid", member_name="../" + FILENAME.removesuffix(".zip") + ".CSV")[0],
            _zip("valid\x00bad")[0],
        )
        for payload in bad_archives:
            with self.subTest(size=len(payload)):
                with self.assertRaises(source.NemwebDispatchPriceError):
                    source.extract_dispatch_price_zip(reference, payload)


if __name__ == "__main__":
    unittest.main()
