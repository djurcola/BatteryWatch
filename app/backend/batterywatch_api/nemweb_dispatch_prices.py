"""Official NEMWeb DispatchIS regional-price artifact adapter."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import re
from urllib.parse import urljoin, urlsplit
import zlib
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, LargeZipFile, ZipFile

from .aemo import parse_dispatch_price_mms_csv
from .nemweb_http import NemwebHttpResource, fetch_nemweb_resource
from .storage import RegionalPrice5m


_INDEX_PATH = "/REPORTS/CURRENT/DispatchIS_Reports/"
_OBSERVED_INDEX_PATH = "/Reports/CURRENT/DispatchIS_Reports/"
DISPATCH_PRICE_INDEX_URL = "https://www.nemweb.com.au" + _INDEX_PATH
DISPATCH_PRICE_INDEX_MAX_BYTES = 2 * 1024 * 1024
DISPATCH_PRICE_ARTIFACT_MAX_BYTES = 16 * 1024 * 1024
_MAX_CSV_BYTES = 16 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_NEM_TIMEZONE = timezone(timedelta(hours=10))
_FILENAME_RE = re.compile(
    r"PUBLIC_DISPATCHIS_(?P<timestamp>[0-9]{12})_(?P<source_id>[0-9]{1,32})\.zip"
)


class NemwebDispatchPriceError(ValueError):
    """Raised when a public DispatchIS source input is not canonical."""


@dataclass(frozen=True, slots=True)
class DispatchPriceArtifactRef:
    url: str
    zip_filename: str
    source_artifact_id: str
    report_timestamp: datetime


@dataclass(frozen=True, slots=True)
class DispatchPriceArtifact:
    reference: DispatchPriceArtifactRef
    csv_member_name: str
    csv_payload: str
    zip_sha256: str
    raw_zip: bytes


@dataclass(frozen=True, slots=True)
class DispatchPriceCollection:
    artifact: DispatchPriceArtifact
    records: tuple[RegionalPrice5m, ...]


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value is not None:
                self.hrefs.append(value)


def discover_dispatch_price_artifacts(
    index_html: str,
    *,
    index_url: str,
) -> tuple[DispatchPriceArtifactRef, ...]:
    """Return canonical current DispatchIS references in chronological order."""

    if index_url != DISPATCH_PRICE_INDEX_URL or not isinstance(index_html, str):
        raise NemwebDispatchPriceError("invalid DispatchIS index")
    try:
        encoded_size = len(index_html.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise NemwebDispatchPriceError("invalid DispatchIS index") from exc
    if encoded_size == 0 or encoded_size > DISPATCH_PRICE_INDEX_MAX_BYTES:
        raise NemwebDispatchPriceError("invalid DispatchIS index")

    parser = _HrefCollector()
    parser.feed(index_html)
    expected_origin = urlsplit(index_url)
    references: dict[str, DispatchPriceArtifactRef] = {}
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
                path
                for path in (_INDEX_PATH, _OBSERVED_INDEX_PATH)
                if parts.path.startswith(path)
            ),
            None,
        )
        if source_path is None:
            continue
        filename = parts.path[len(source_path):]
        if not filename or "/" in filename or parts.query or parts.fragment:
            continue
        match = _FILENAME_RE.fullmatch(filename)
        if match is None:
            continue
        try:
            report_timestamp = datetime.strptime(
                match.group("timestamp"),
                "%Y%m%d%H%M",
            ).replace(tzinfo=_NEM_TIMEZONE).astimezone(timezone.utc)
        except (OverflowError, ValueError):
            continue
        source_id = match.group("source_id")
        canonical_url = DISPATCH_PRICE_INDEX_URL + filename
        existing_url = source_urls.get(source_id)
        if existing_url is not None and existing_url != canonical_url:
            raise NemwebDispatchPriceError("conflicting DispatchIS artifact references")
        source_urls[source_id] = canonical_url
        references[canonical_url] = DispatchPriceArtifactRef(
            url=canonical_url,
            zip_filename=filename,
            source_artifact_id=source_id,
            report_timestamp=report_timestamp,
        )

    if not references:
        raise NemwebDispatchPriceError("invalid DispatchIS index")
    return tuple(sorted(
        references.values(),
        key=lambda item: (
            item.report_timestamp,
            int(item.source_artifact_id),
            item.zip_filename,
        ),
    ))


def extract_dispatch_price_zip(
    reference: DispatchPriceArtifactRef,
    zip_payload: bytes,
) -> DispatchPriceArtifact:
    """Extract one canonical DispatchIS CSV member with bounded resources."""

    if (
        not isinstance(reference, DispatchPriceArtifactRef)
        or type(zip_payload) is not bytes
        or not zip_payload
        or len(zip_payload) > DISPATCH_PRICE_ARTIFACT_MAX_BYTES
    ):
        raise NemwebDispatchPriceError("invalid DispatchIS ZIP")
    match = _FILENAME_RE.fullmatch(reference.zip_filename)
    if match is None:
        raise NemwebDispatchPriceError("invalid DispatchIS ZIP")
    try:
        expected_timestamp = datetime.strptime(
            match.group("timestamp"),
            "%Y%m%d%H%M",
        ).replace(tzinfo=_NEM_TIMEZONE).astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise NemwebDispatchPriceError("invalid DispatchIS ZIP") from exc
    if (
        reference.url != DISPATCH_PRICE_INDEX_URL + reference.zip_filename
        or reference.source_artifact_id != match.group("source_id")
        or reference.report_timestamp != expected_timestamp
    ):
        raise NemwebDispatchPriceError("invalid DispatchIS ZIP")

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
                raise NemwebDispatchPriceError("invalid DispatchIS ZIP")
            member = members[0]
            if (
                member.flag_bits & 1
                or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                or member.file_size <= 0
                or member.file_size > _MAX_CSV_BYTES
                or member.compress_size <= 0
                or member.file_size > member.compress_size * _MAX_COMPRESSION_RATIO
            ):
                raise NemwebDispatchPriceError("invalid DispatchIS ZIP")
            with archive.open(member) as member_stream:
                raw_csv = member_stream.read(_MAX_CSV_BYTES + 1)
                has_more_data = bool(member_stream.read(1))
            if (
                len(raw_csv) > _MAX_CSV_BYTES
                or has_more_data
                or len(raw_csv) != member.file_size
            ):
                raise NemwebDispatchPriceError("invalid DispatchIS ZIP")
            csv_payload = raw_csv.decode("utf-8-sig")
            if not csv_payload or "\x00" in csv_payload:
                raise NemwebDispatchPriceError("invalid DispatchIS ZIP")
    except NemwebDispatchPriceError:
        raise
    except (
        BadZipFile,
        EOFError,
        LargeZipFile,
        NotImplementedError,
        RuntimeError,
        UnicodeDecodeError,
        zlib.error,
    ) as exc:
        raise NemwebDispatchPriceError("invalid DispatchIS ZIP") from exc

    return DispatchPriceArtifact(
        reference=reference,
        csv_member_name=member.filename,
        csv_payload=csv_payload,
        zip_sha256=sha256(zip_payload).hexdigest(),
        raw_zip=zip_payload,
    )


def collect_latest_dispatch_prices(
    ingestion_version: int,
    correction_version: int = 0,
    *,
    fetch: Callable[..., NemwebHttpResource] = fetch_nemweb_resource,
) -> DispatchPriceCollection:
    """Fetch, verify and parse the latest official five-region price report."""

    index_resource = fetch(
        DISPATCH_PRICE_INDEX_URL,
        max_bytes=DISPATCH_PRICE_INDEX_MAX_BYTES,
    )
    try:
        index_html = index_resource.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NemwebDispatchPriceError("invalid DispatchIS index") from exc
    references = discover_dispatch_price_artifacts(
        index_html,
        index_url=DISPATCH_PRICE_INDEX_URL,
    )
    latest = references[-1]
    artifact_resource = fetch(
        latest.url,
        max_bytes=DISPATCH_PRICE_ARTIFACT_MAX_BYTES,
    )
    artifact = extract_dispatch_price_zip(latest, artifact_resource.body)
    records = parse_dispatch_price_mms_csv(
        artifact.csv_payload,
        source_id=artifact.reference.source_artifact_id,
        ingestion_version=ingestion_version,
        correction_version=correction_version,
    )
    return DispatchPriceCollection(artifact=artifact, records=records)


__all__ = [
    "DISPATCH_PRICE_ARTIFACT_MAX_BYTES",
    "DISPATCH_PRICE_INDEX_MAX_BYTES",
    "DISPATCH_PRICE_INDEX_URL",
    "DispatchPriceArtifact",
    "DispatchPriceArtifactRef",
    "DispatchPriceCollection",
    "NemwebDispatchPriceError",
    "collect_latest_dispatch_prices",
    "discover_dispatch_price_artifacts",
    "extract_dispatch_price_zip",
]
