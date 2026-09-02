"""Tests for one latest Dispatch SCADA collection cycle."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from batterywatch_api.collector import (
    DISPATCH_SCADA_ARTIFACT_MAX_BYTES,
    DISPATCH_SCADA_INDEX_MAX_BYTES,
    DISPATCH_SCADA_INDEX_URL,
    DispatchScadaCollection,
    collect_latest_dispatch_scada,
)
from batterywatch_api.nemweb_dispatch_scada import (
    DispatchScadaArtifact,
    DispatchScadaArtifactRef,
)
from batterywatch_api.nemweb_http import NemwebHttpResource
from batterywatch_api.storage import GeneratorPower5m


NEWER_FILENAME = "PUBLIC_DISPATCHSCADA_202608291205_0000000000000002.zip"
NEWER_URL = DISPATCH_SCADA_INDEX_URL + NEWER_FILENAME
NEWER_SOURCE_ID = "0000000000000002"


@dataclass
class FakeFetch:
    resources: dict[str, NemwebHttpResource]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url: str, *, max_bytes: int) -> NemwebHttpResource:
        self.calls.append((url, max_bytes))
        return self.resources[url]


def _make_resource(url: str, body: bytes, content_type: str) -> NemwebHttpResource:
    return NemwebHttpResource(
        requested_url=url,
        resolved_url=url,
        body=body,
        content_type=content_type,
        etag=None,
        last_modified=None,
    )


def _make_zip(filename: str, csv_payload: str) -> tuple[bytes, str]:
    member_name = filename.removesuffix(".zip") + ".CSV"
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(member_name, csv_payload.encode("utf-8"))
    return buffer.getvalue(), member_name


class CollectLatestDispatchScadaTests(unittest.TestCase):
    def test_collects_latest_canonical_artifact_and_records(self) -> None:
        index_html = f"""
        <html><body>
          <a href="{NEWER_FILENAME}">newer</a>
          <a href="PUBLIC_DISPATCHSCADA_202608291200_0000000000000001.zip">older</a>
        </body></html>
        """
        csv_payload = f"""C,NEMP.WORLD,DISPATCHSCADA,AEMO,PUBLIC,2026/08/29,12:05:15,{NEWER_SOURCE_ID},DISPATCHSCADA,0000000000000001
I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE,LASTCHANGED
D,DISPATCH,UNIT_SCADA,1,"2026/08/29 12:05:00",BWTR2,0,"2026/08/29 12:05:11"
D,DISPATCH,UNIT_SCADA,1,"2026/08/29 12:00:00",BWTR1,-4.5,"2026/08/29 12:00:11"
C,END OF REPORT,5
"""
        zip_payload, member_name = _make_zip(NEWER_FILENAME, csv_payload)
        fetch = FakeFetch(
            resources={
                DISPATCH_SCADA_INDEX_URL: _make_resource(
                    DISPATCH_SCADA_INDEX_URL,
                    index_html.encode("utf-8"),
                    "text/html; charset=utf-8",
                ),
                NEWER_URL: _make_resource(
                    NEWER_URL,
                    zip_payload,
                    "application/zip",
                ),
            }
        )

        reference = DispatchScadaArtifactRef(
            url=NEWER_URL,
            zip_filename=NEWER_FILENAME,
            source_artifact_id=NEWER_SOURCE_ID,
            report_timestamp=datetime(2026, 8, 29, 2, 5, tzinfo=timezone.utc),
        )
        expected = (
            DispatchScadaCollection(
                artifact=DispatchScadaArtifact(
                    reference=reference,
                    csv_member_name=member_name,
                    csv_payload=csv_payload,
                    zip_sha256=sha256(zip_payload).hexdigest(),
                    raw_zip=zip_payload,
                ),
                records=(
                    GeneratorPower5m(
                        generator_id="BWTR1",
                        interval_start=datetime(
                            2026, 8, 29, 2, 0, tzinfo=timezone.utc
                        ),
                        power_mw=-4.5,
                        source_id=NEWER_SOURCE_ID,
                        source_timestamp=datetime(
                            2026, 8, 29, 2, 0, 11, tzinfo=timezone.utc
                        ),
                        ingestion_version=7,
                        correction_version=2,
                    ),
                    GeneratorPower5m(
                        generator_id="BWTR2",
                        interval_start=datetime(
                            2026, 8, 29, 2, 5, tzinfo=timezone.utc
                        ),
                        power_mw=0.0,
                        source_id=NEWER_SOURCE_ID,
                        source_timestamp=datetime(
                            2026, 8, 29, 2, 5, 11, tzinfo=timezone.utc
                        ),
                        ingestion_version=7,
                        correction_version=2,
                    ),
                ),
            ),
            (
                (DISPATCH_SCADA_INDEX_URL, DISPATCH_SCADA_INDEX_MAX_BYTES),
                (NEWER_URL, DISPATCH_SCADA_ARTIFACT_MAX_BYTES),
            ),
        )

        result = collect_latest_dispatch_scada(
            ingestion_version=7,
            correction_version=2,
            fetch=fetch,
        )

        self.assertEqual((result, tuple(fetch.calls)), expected)


if __name__ == "__main__":
    unittest.main()
