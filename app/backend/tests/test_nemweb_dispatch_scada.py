"""Tests for strict NEMWeb Dispatch SCADA source discovery."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import struct
from typing import Any
import unittest
from unittest.mock import patch
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile

import batterywatch_api.nemweb_dispatch_scada as source


INDEX_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"


def _make_zip(member_name: str, payload: bytes) -> bytes:
    return _make_zip_members((member_name, payload))


def _make_zip_members(
    *members: tuple[str, bytes], compression: int = ZIP_DEFLATED
) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=compression) as archive:
        for member_name, payload in members:
            archive.writestr(member_name, payload)
    return buffer.getvalue()


def _suffix_raw_zip_member_name(zip_payload: bytes, suffix: bytes) -> bytes:
    mutated = bytearray(zip_payload)
    local_name_length = struct.unpack_from("<H", mutated, 26)[0]
    local_insert = 30 + local_name_length
    mutated[local_insert:local_insert] = suffix
    struct.pack_into("<H", mutated, 26, local_name_length + len(suffix))

    central_offset = mutated.find(b"PK\x01\x02")
    central_name_length = struct.unpack_from("<H", mutated, central_offset + 28)[0]
    central_insert = central_offset + 46 + central_name_length
    mutated[central_insert:central_insert] = suffix
    struct.pack_into(
        "<H", mutated, central_offset + 28, central_name_length + len(suffix)
    )

    end_offset = mutated.find(b"PK\x05\x06")
    central_size = struct.unpack_from("<L", mutated, end_offset + 12)[0]
    central_start = struct.unpack_from("<L", mutated, end_offset + 16)[0]
    struct.pack_into("<L", mutated, end_offset + 12, central_size + len(suffix))
    struct.pack_into("<L", mutated, end_offset + 16, central_start + len(suffix))
    return bytes(mutated)


def _corrupt_first_compressed_byte(zip_payload: bytes) -> bytes:
    mutated = bytearray(zip_payload)
    name_length = struct.unpack_from("<H", mutated, 26)[0]
    extra_length = struct.unpack_from("<H", mutated, 28)[0]
    data_offset = 30 + name_length + extra_length
    mutated[data_offset] = 0xFF
    return bytes(mutated)


def _mark_zip_member_encrypted(zip_payload: bytes) -> bytes:
    mutated = bytearray(zip_payload)
    local_flags = struct.unpack_from("<H", mutated, 6)[0] | 0x1
    struct.pack_into("<H", mutated, 6, local_flags)
    central_offset = mutated.find(b"PK\x01\x02")
    central_flags = struct.unpack_from("<H", mutated, central_offset + 8)[0] | 0x1
    struct.pack_into("<H", mutated, central_offset + 8, central_flags)
    return bytes(mutated)


class DiscoverDispatchScadaArtifactsTests(unittest.TestCase):
    def test_discovers_deduplicates_and_sorts_canonical_links(self) -> None:
        index_html = """
        <html><body>
          <a href="readme.txt">unrelated</a>
          <a href="https://example.invalid/PUBLIC_DISPATCHSCADA_202501011200_99.zip">other origin</a>
          <a href="PUBLIC_DISPATCHSCADA_202501020304_20.zip">later</a>
          <a href="PUBLIC_DISPATCHSCADA_202501020304_20.zip">duplicate</a>
          <a href="./PUBLIC_DISPATCHSCADA_202501011200_3.zip">earlier</a>
        </body></html>
        """
        discover = getattr(source, "discover_dispatch_scada_artifacts", lambda *_args, **_kwargs: ())

        actual = discover(index_html, index_url=INDEX_URL)

        self.assertEqual(
            tuple(
                (
                    item.url,
                    item.zip_filename,
                    item.source_artifact_id,
                    item.report_timestamp,
                )
                for item in actual
            ),
            (
                (
                    INDEX_URL + "PUBLIC_DISPATCHSCADA_202501011200_3.zip",
                    "PUBLIC_DISPATCHSCADA_202501011200_3.zip",
                    "3",
                    datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
                ),
                (
                    INDEX_URL + "PUBLIC_DISPATCHSCADA_202501020304_20.zip",
                    "PUBLIC_DISPATCHSCADA_202501020304_20.zip",
                    "20",
                    datetime(2025, 1, 1, 17, 4, tzinfo=timezone.utc),
                ),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            actual[0].url = "changed"  # type: ignore[misc]

    def test_rejects_empty_index_with_public_error(self) -> None:
        public_error = getattr(source, "NemwebDispatchScadaError", AssertionError)

        with self.assertRaises(public_error):
            source.discover_dispatch_scada_artifacts("", index_url=INDEX_URL)

    def test_rejects_noncanonical_index_url(self) -> None:
        index_html = '<a href="PUBLIC_DISPATCHSCADA_202501011200_3.zip">file</a>'

        for index_url in (
            "http://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/",
            "https://nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/",
            "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/?page=1",
            "https://www.nemweb.com.au/REPORTS/ARCHIVE/Dispatch_SCADA/",
        ):
            with self.subTest(index_url=index_url):
                with self.assertRaises(source.NemwebDispatchScadaError):
                    source.discover_dispatch_scada_artifacts(
                        index_html, index_url=index_url
                    )

    def test_normalizes_invalid_index_payloads_to_public_error(self) -> None:
        invalid_payloads: tuple[Any, ...] = (
            None,
            b"not text",
            "x" * (2 * 1024 * 1024 + 1),
            "\ud800",
        )

        for index_html in invalid_payloads:
            with self.subTest(payload_type=type(index_html).__name__):
                try:
                    source.discover_dispatch_scada_artifacts(
                        index_html,  # type: ignore[arg-type]
                        index_url=INDEX_URL,
                    )
                except Exception as error:
                    error_type: type[BaseException] | None = type(error)
                else:
                    error_type = None
                self.assertIs(error_type, source.NemwebDispatchScadaError)

    def test_rejects_index_without_canonical_artifacts(self) -> None:
        index_html = """
        <a href="readme.txt">unrelated</a>
        <a href="nested/PUBLIC_DISPATCHSCADA_202501011200_3.zip">nested</a>
        <a href="PUBLIC_DISPATCHSCADA_202502301200_4.zip">bad date</a>
        """

        with self.assertRaises(source.NemwebDispatchScadaError):
            source.discover_dispatch_scada_artifacts(
                index_html,
                index_url=INDEX_URL,
            )

    def test_rejects_conflicting_source_sequence(self) -> None:
        index_html = """
        <a href="PUBLIC_DISPATCHSCADA_202501011200_3.zip">first</a>
        <a href="PUBLIC_DISPATCHSCADA_202501011205_3.zip">conflict</a>
        """

        with self.assertRaises(source.NemwebDispatchScadaError):
            source.discover_dispatch_scada_artifacts(
                index_html,
                index_url=INDEX_URL,
            )

    def test_accepts_observed_official_root_relative_href(self) -> None:
        filename = "PUBLIC_DISPATCHSCADA_202608271535_0000000534726419.zip"
        index_html = (
            '<a href="/Reports/CURRENT/Dispatch_SCADA/'
            + filename
            + '">artifact</a>'
        )

        try:
            references = source.discover_dispatch_scada_artifacts(
                index_html,
                index_url=INDEX_URL,
            )
        except source.NemwebDispatchScadaError:
            actual: tuple[tuple[str, str], ...] = ()
        else:
            actual = tuple((item.url, item.zip_filename) for item in references)

        self.assertEqual(actual, ((INDEX_URL + filename, filename),))

    def test_ignores_malformed_href_without_losing_valid_artifact(self) -> None:
        filename = "PUBLIC_DISPATCHSCADA_202501011200_3.zip"
        index_html = (
            '<a href="https://[::1">malformed</a>'
            f'<a href="{filename}">valid</a>'
        )

        try:
            references = source.discover_dispatch_scada_artifacts(
                index_html,
                index_url=INDEX_URL,
            )
        except Exception as error:
            actual: tuple[tuple[str, str], ...] = ((type(error).__name__, ""),)
        else:
            actual = tuple((item.url, item.zip_filename) for item in references)

        self.assertEqual(actual, ((INDEX_URL + filename, filename),))

    def test_normalizes_oversized_numeric_source_id_to_public_error(self) -> None:
        index_html = (
            '<a href="PUBLIC_DISPATCHSCADA_202501011200_'
            + ("7" * 4301)
            + '.zip">oversized sequence</a>'
        )

        try:
            source.discover_dispatch_scada_artifacts(index_html, index_url=INDEX_URL)
        except Exception as error:
            error_type: type[BaseException] | None = type(error)
        else:
            error_type = None

        self.assertIs(error_type, source.NemwebDispatchScadaError)

    def test_normalizes_utc_underflow_timestamp_to_public_error(self) -> None:
        index_html = (
            '<a href="PUBLIC_DISPATCHSCADA_000101010000_3.zip">underflow</a>'
        )

        try:
            source.discover_dispatch_scada_artifacts(index_html, index_url=INDEX_URL)
        except Exception as error:
            error_type: type[BaseException] | None = type(error)
        else:
            error_type = None

        self.assertIs(error_type, source.NemwebDispatchScadaError)

    def test_filters_external_origin_using_protected_path(self) -> None:
        index_html = (
            '<a href="https://evil.example/REPORTS/CURRENT/Dispatch_SCADA/'
            'PUBLIC_DISPATCHSCADA_202501011200_3.zip">external</a>'
        )

        with self.assertRaises(source.NemwebDispatchScadaError):
            source.discover_dispatch_scada_artifacts(
                index_html,
                index_url=INDEX_URL,
            )

    def test_orders_equal_timestamps_by_numeric_source_id(self) -> None:
        index_html = """
        <a href="PUBLIC_DISPATCHSCADA_202501011200_12.zip">twelve</a>
        <a href="PUBLIC_DISPATCHSCADA_202501011200_3.zip">three</a>
        """

        references = source.discover_dispatch_scada_artifacts(
            index_html,
            index_url=INDEX_URL,
        )

        self.assertEqual(
            tuple(item.source_artifact_id for item in references),
            ("3", "12"),
        )


class ExtractDispatchScadaZipTests(unittest.TestCase):
    def test_extracts_one_canonical_member_with_immutable_provenance(self) -> None:
        reference = source.DispatchScadaArtifactRef(
            url=(
                INDEX_URL
                + "PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip"
            ),
            zip_filename="PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip",
            source_artifact_id="0000000000000003",
            report_timestamp=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
        )
        member_name = reference.zip_filename.removesuffix(".zip") + ".CSV"
        csv_payload = b"C,NEMP.WORLD,DISPATCHSCADA\r\n"
        zip_payload = _make_zip(member_name, csv_payload)
        artifact = source.extract_dispatch_scada_zip(reference, zip_payload)
        actual = (
            artifact.reference,
            artifact.csv_member_name,
            artifact.csv_payload,
            artifact.zip_sha256,
            artifact.raw_zip,
        )

        self.assertEqual(
            actual,
            (
                reference,
                member_name,
                csv_payload.decode("utf-8"),
                sha256(zip_payload).hexdigest(),
                zip_payload,
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            artifact.csv_payload = "changed"  # type: ignore[misc]

    def test_normalizes_invalid_zip_public_inputs(self) -> None:
        reference = source.DispatchScadaArtifactRef(
            url=INDEX_URL + "PUBLIC_DISPATCHSCADA_202501011200_3.zip",
            zip_filename="PUBLIC_DISPATCHSCADA_202501011200_3.zip",
            source_artifact_id="3",
            report_timestamp=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
        )
        member_name = reference.zip_filename.removesuffix(".zip") + ".CSV"
        valid_zip = _make_zip(member_name, b"C,NEMP.WORLD,DISPATCHSCADA\r\n")
        cases: tuple[tuple[Any, Any], ...] = (
            (object(), valid_zip),
            (reference, "not bytes"),
            (reference, b""),
            (reference, b"x" * ((16 * 1024 * 1024) + 1)),
        )

        for bad_reference, bad_payload in cases:
            with self.subTest(
                reference_type=type(bad_reference).__name__,
                payload_type=type(bad_payload).__name__,
            ):
                try:
                    source.extract_dispatch_scada_zip(
                        bad_reference,  # type: ignore[arg-type]
                        bad_payload,  # type: ignore[arg-type]
                    )
                except Exception as error:
                    error_type: type[BaseException] | None = type(error)
                else:
                    error_type = None
                self.assertIs(error_type, source.NemwebDispatchScadaError)


    def test_rejects_invalid_archive_structure_with_public_error(self) -> None:
        reference = source.DispatchScadaArtifactRef(
            url=INDEX_URL + "PUBLIC_DISPATCHSCADA_202501011200_3.zip",
            zip_filename="PUBLIC_DISPATCHSCADA_202501011200_3.zip",
            source_artifact_id="3",
            report_timestamp=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
        )
        expected_member = reference.zip_filename.removesuffix(".zip") + ".CSV"
        cases = (
            b"not a zip",
            _make_zip_members(),
            _make_zip_members(
                (expected_member, b"valid"),
                ("extra.txt", b"extra"),
            ),
            _make_zip("WRONG.CSV", b"wrong"),
            _make_zip(expected_member.lower(), b"wrong case"),
            _make_zip("../" + expected_member, b"traversal"),
            _make_zip("nested/" + expected_member, b"nested"),
        )

        for zip_payload in cases:
            with self.subTest(size=len(zip_payload)):
                try:
                    source.extract_dispatch_scada_zip(reference, zip_payload)
                except Exception as error:
                    error_type: type[BaseException] | None = type(error)
                else:
                    error_type = None
                self.assertIs(error_type, source.NemwebDispatchScadaError)


    def test_rejects_unsafe_member_content_with_public_error(self) -> None:
        reference = source.DispatchScadaArtifactRef(
            url=INDEX_URL + "PUBLIC_DISPATCHSCADA_202501011200_3.zip",
            zip_filename="PUBLIC_DISPATCHSCADA_202501011200_3.zip",
            source_artifact_id="3",
            report_timestamp=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
        )
        expected_member = reference.zip_filename.removesuffix(".zip") + ".CSV"
        cases = (
            _make_zip(expected_member, b""),
            _make_zip(expected_member, b"\xff"),
            _make_zip(expected_member, b"valid\x00invalid"),
            _make_zip_members(
                (expected_member, b"unsupported compression"),
                compression=ZIP_BZIP2,
            ),
            _make_zip(expected_member, b"A" * (1024 * 1024)),
            _make_zip(expected_member, b"B" * ((8 * 1024 * 1024) + 1)),
        )

        for zip_payload in cases:
            with self.subTest(size=len(zip_payload)):
                try:
                    source.extract_dispatch_scada_zip(reference, zip_payload)
                except Exception as error:
                    error_type: type[BaseException] | None = type(error)
                else:
                    error_type = None
                self.assertIs(error_type, source.NemwebDispatchScadaError)


    def test_rejects_forged_artifact_reference_provenance(self) -> None:
        reference = source.DispatchScadaArtifactRef(
            url=INDEX_URL + "PUBLIC_DISPATCHSCADA_202501011200_3.zip",
            zip_filename="PUBLIC_DISPATCHSCADA_202501011200_3.zip",
            source_artifact_id="3",
            report_timestamp=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
        )
        member_name = reference.zip_filename.removesuffix(".zip") + ".CSV"
        zip_payload = _make_zip(member_name, b"C,NEMP.WORLD,DISPATCHSCADA\r\n")
        cases = (
            replace(reference, url="https://evil.example/" + reference.zip_filename),
            replace(reference, source_artifact_id="4"),
            replace(
                reference,
                report_timestamp=datetime(2025, 1, 1, 2, 5, tzinfo=timezone.utc),
            ),
        )

        for forged_reference in cases:
            with self.subTest(reference=forged_reference):
                try:
                    source.extract_dispatch_scada_zip(forged_reference, zip_payload)
                except Exception as error:
                    error_type: type[BaseException] | None = type(error)
                else:
                    error_type = None
                self.assertIs(error_type, source.NemwebDispatchScadaError)


    def test_rejects_raw_nul_suffixed_member_name(self) -> None:
        reference = source.DispatchScadaArtifactRef(
            url=(
                INDEX_URL
                + "PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip"
            ),
            zip_filename="PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip",
            source_artifact_id="0000000000000003",
            report_timestamp=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
        )
        member_name = reference.zip_filename.removesuffix(".zip") + ".CSV"
        zip_payload = _suffix_raw_zip_member_name(
            _make_zip(member_name, b"payload"), b"\x00evil"
        )

        with self.assertRaises(source.NemwebDispatchScadaError):
            source.extract_dispatch_scada_zip(reference, zip_payload)


    def test_normalizes_malformed_deflate_to_public_error(self) -> None:
        reference = source.DispatchScadaArtifactRef(
            url=(
                INDEX_URL
                + "PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip"
            ),
            zip_filename="PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip",
            source_artifact_id="0000000000000003",
            report_timestamp=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
        )
        member_name = reference.zip_filename.removesuffix(".zip") + ".CSV"
        zip_payload = _corrupt_first_compressed_byte(
            _make_zip(member_name, b"payload")
        )

        error_type: type[BaseException] | None = None
        try:
            source.extract_dispatch_scada_zip(reference, zip_payload)
        except BaseException as error:
            error_type = type(error)

        self.assertIs(error_type, source.NemwebDispatchScadaError)


    def test_normalizes_crc_and_encryption_failures_to_public_error(self) -> None:
        reference = source.DispatchScadaArtifactRef(
            url=(
                INDEX_URL
                + "PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip"
            ),
            zip_filename="PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip",
            source_artifact_id="0000000000000003",
            report_timestamp=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
        )
        member_name = reference.zip_filename.removesuffix(".zip") + ".CSV"
        stored_zip = _make_zip_members(
            (member_name, b"payload"), compression=ZIP_STORED
        )
        cases = (
            _corrupt_first_compressed_byte(stored_zip),
            _mark_zip_member_encrypted(_make_zip(member_name, b"payload")),
        )

        for zip_payload in cases:
            with self.subTest():
                with self.assertRaises(source.NemwebDispatchScadaError):
                    source.extract_dispatch_scada_zip(reference, zip_payload)


    def test_normalizes_zip_stream_eof_to_public_error(self) -> None:
        class EofZipFile:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> "EofZipFile":
                raise EOFError

            def __exit__(self, *_args: object) -> None:
                return None

        reference = source.DispatchScadaArtifactRef(
            url=(
                INDEX_URL
                + "PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip"
            ),
            zip_filename="PUBLIC_DISPATCHSCADA_202501011200_0000000000000003.zip",
            source_artifact_id="0000000000000003",
            report_timestamp=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
        )
        member_name = reference.zip_filename.removesuffix(".zip") + ".CSV"

        error_type: type[BaseException] | None = None
        with patch.object(source, "ZipFile", EofZipFile):
            try:
                source.extract_dispatch_scada_zip(
                    reference, _make_zip(member_name, b"payload")
                )
            except BaseException as error:
                error_type = type(error)

        self.assertIs(error_type, source.NemwebDispatchScadaError)


if __name__ == "__main__":
    unittest.main()
