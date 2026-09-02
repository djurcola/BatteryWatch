"""Pure source metadata handling for NEMWeb Dispatch SCADA artifacts."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import re
from urllib.parse import urljoin, urlsplit
import zlib
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, LargeZipFile, ZipFile


_INDEX_PATH = "/REPORTS/CURRENT/Dispatch_SCADA/"
_OBSERVED_INDEX_PATH = "/Reports/CURRENT/Dispatch_SCADA/"
_OFFICIAL_INDEX_URL = "https://www.nemweb.com.au" + _INDEX_PATH
_MAX_INDEX_BYTES = 2 * 1024 * 1024
_MAX_ZIP_BYTES = 16 * 1024 * 1024
_MAX_CSV_BYTES = 8 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_NEM_TIMEZONE = timezone(timedelta(hours=10))
_FILENAME_RE = re.compile(
    r"PUBLIC_DISPATCHSCADA_(?P<timestamp>[0-9]{12})_(?P<source_id>[0-9]{1,32})\.zip"
)


@dataclass(frozen=True)
class DispatchScadaArtifactRef:
    """Canonical identity for one current Dispatch SCADA ZIP."""

    url: str
    zip_filename: str
    source_artifact_id: str
    report_timestamp: datetime


class NemwebDispatchScadaError(ValueError):
    """Raised when a public NEMWeb artifact source input is not canonical."""


@dataclass(frozen=True)
class DispatchScadaArtifact:
    """One verified ZIP and decoded CSV member with immutable provenance."""

    reference: DispatchScadaArtifactRef
    csv_member_name: str
    csv_payload: str
    zip_sha256: str
    raw_zip: bytes


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value is not None:
                self.hrefs.append(value)


def discover_dispatch_scada_artifacts(
    index_html: str, *, index_url: str
) -> tuple[DispatchScadaArtifactRef, ...]:
    """Return canonical current Dispatch SCADA references in source order."""

    if index_url != _OFFICIAL_INDEX_URL or type(index_html) is not str:
        raise NemwebDispatchScadaError("invalid Dispatch SCADA index")
    try:
        encoded_size = len(index_html.encode("utf-8"))
    except UnicodeEncodeError:
        raise NemwebDispatchScadaError("invalid Dispatch SCADA index") from None
    if encoded_size == 0 or encoded_size > _MAX_INDEX_BYTES:
        raise NemwebDispatchScadaError("invalid Dispatch SCADA index")

    parser = _HrefCollector()
    parser.feed(index_html)
    expected_origin = urlsplit(index_url)
    references: dict[str, DispatchScadaArtifactRef] = {}
    source_urls: dict[str, str] = {}

    for href in parser.hrefs:
        try:
            url = urljoin(index_url, href)
            parts = urlsplit(url)
        except ValueError:
            continue
        if (parts.scheme, parts.netloc) != (expected_origin.scheme, expected_origin.netloc):
            continue
        source_path = next(
            (
                candidate
                for candidate in (_INDEX_PATH, _OBSERVED_INDEX_PATH)
                if parts.path.startswith(candidate)
            ),
            None,
        )
        if source_path is None:
            continue
        filename = parts.path[len(source_path) :]
        if not filename or "/" in filename or parts.query or parts.fragment:
            continue
        match = _FILENAME_RE.fullmatch(filename)
        if match is None:
            continue
        try:
            report_timestamp = datetime.strptime(
                match.group("timestamp"), "%Y%m%d%H%M"
            ).replace(tzinfo=_NEM_TIMEZONE).astimezone(timezone.utc)
        except (ValueError, OverflowError):
            continue
        source_id = match.group("source_id")
        canonical_url = _OFFICIAL_INDEX_URL + filename
        existing_url = source_urls.get(source_id)
        if existing_url is not None and existing_url != canonical_url:
            raise NemwebDispatchScadaError(
                "conflicting Dispatch SCADA artifact references"
            )
        source_urls[source_id] = canonical_url
        references[canonical_url] = DispatchScadaArtifactRef(
            url=canonical_url,
            zip_filename=filename,
            source_artifact_id=source_id,
            report_timestamp=report_timestamp,
        )

    if not references:
        raise NemwebDispatchScadaError("invalid Dispatch SCADA index")

    return tuple(
        sorted(
            references.values(),
            key=lambda item: (
                item.report_timestamp,
                int(item.source_artifact_id),
                item.zip_filename,
            ),
        )
    )


def extract_dispatch_scada_zip(
    reference: DispatchScadaArtifactRef,
    zip_payload: bytes,
) -> DispatchScadaArtifact:
    """Extract one canonical Dispatch SCADA CSV member."""

    if (
        type(reference) is not DispatchScadaArtifactRef
        or type(zip_payload) is not bytes
        or not zip_payload
        or len(zip_payload) > _MAX_ZIP_BYTES
    ):
        raise NemwebDispatchScadaError("invalid Dispatch SCADA ZIP")
    match = _FILENAME_RE.fullmatch(reference.zip_filename)
    if match is None:
        raise NemwebDispatchScadaError("invalid Dispatch SCADA ZIP")
    try:
        expected_timestamp = datetime.strptime(
            match.group("timestamp"), "%Y%m%d%H%M"
        ).replace(tzinfo=_NEM_TIMEZONE).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise NemwebDispatchScadaError("invalid Dispatch SCADA ZIP") from None
    if (
        reference.url != _OFFICIAL_INDEX_URL + reference.zip_filename
        or reference.source_artifact_id != match.group("source_id")
        or reference.report_timestamp != expected_timestamp
    ):
        raise NemwebDispatchScadaError("invalid Dispatch SCADA ZIP")
    try:
        with ZipFile(BytesIO(zip_payload)) as archive:
            members = archive.infolist()
            expected_member = reference.zip_filename.removesuffix(".zip") + ".CSV"
            if (
                len(members) != 1
                or members[0].is_dir()
                or members[0].filename != expected_member
                or members[0].orig_filename != expected_member
            ):
                raise NemwebDispatchScadaError("invalid Dispatch SCADA ZIP")
            member = members[0]
            if (
                member.flag_bits & 1
                or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                or member.file_size <= 0
                or member.file_size > _MAX_CSV_BYTES
                or member.compress_size <= 0
                or member.file_size
                > member.compress_size * _MAX_COMPRESSION_RATIO
            ):
                raise NemwebDispatchScadaError("invalid Dispatch SCADA ZIP")
            with archive.open(member) as member_stream:
                raw_csv = member_stream.read(_MAX_CSV_BYTES + 1)
                has_more_data = bool(member_stream.read(1))
            if (
                len(raw_csv) > _MAX_CSV_BYTES
                or has_more_data
                or len(raw_csv) != member.file_size
            ):
                raise NemwebDispatchScadaError("invalid Dispatch SCADA ZIP")
            payload = raw_csv.decode("utf-8-sig")
            if not payload or "\x00" in payload:
                raise NemwebDispatchScadaError("invalid Dispatch SCADA ZIP")
    except NemwebDispatchScadaError:
        raise
    except (
        BadZipFile,
        EOFError,
        LargeZipFile,
        RuntimeError,
        UnicodeDecodeError,
        NotImplementedError,
        zlib.error,
    ):
        raise NemwebDispatchScadaError("invalid Dispatch SCADA ZIP") from None
    return DispatchScadaArtifact(
        reference=reference,
        csv_member_name=member.filename,
        csv_payload=payload,
        zip_sha256=sha256(zip_payload).hexdigest(),
        raw_zip=zip_payload,
    )
