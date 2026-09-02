"""One latest Dispatch SCADA collection cycle."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta, timezone

from .dispatch_scada import parse_dispatch_scada_csv
from .nemweb_dispatch_scada import (
    DispatchScadaArtifact,
    discover_dispatch_scada_artifacts,
    extract_dispatch_scada_zip,
)
from .nemweb_http import NemwebHttpResource, fetch_nemweb_resource
from .storage import GeneratorPower5m


DISPATCH_SCADA_INDEX_URL = (
    "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
)
DISPATCH_SCADA_INDEX_MAX_BYTES = 2 * 1024 * 1024
DISPATCH_SCADA_ARTIFACT_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DispatchScadaCollection:
    """The verified latest artifact and its parsed power records."""

    artifact: DispatchScadaArtifact
    records: tuple[GeneratorPower5m, ...]


def collect_latest_dispatch_scada(
    ingestion_version: int,
    correction_version: int = 0,
    *,
    fetch: Callable[..., NemwebHttpResource] = fetch_nemweb_resource,
) -> DispatchScadaCollection:
    """Collect the latest canonical Dispatch SCADA artifact."""

    index_resource = fetch(
        DISPATCH_SCADA_INDEX_URL,
        max_bytes=DISPATCH_SCADA_INDEX_MAX_BYTES,
    )
    references = discover_dispatch_scada_artifacts(
        index_resource.body.decode("utf-8"),
        index_url=DISPATCH_SCADA_INDEX_URL,
    )
    latest_reference = references[-1]
    artifact_resource = fetch(
        latest_reference.url,
        max_bytes=DISPATCH_SCADA_ARTIFACT_MAX_BYTES,
    )
    artifact = extract_dispatch_scada_zip(
        latest_reference,
        artifact_resource.body,
    )
    records = parse_dispatch_scada_csv(
        artifact.csv_payload,
        source_artifact_id=artifact.reference.source_artifact_id,
        ingestion_version=ingestion_version,
        correction_version=correction_version,
        naive_timezone=timezone(timedelta(hours=10)),
    )
    return DispatchScadaCollection(artifact=artifact, records=records)


__all__ = [
    "DISPATCH_SCADA_ARTIFACT_MAX_BYTES",
    "DISPATCH_SCADA_INDEX_MAX_BYTES",
    "DISPATCH_SCADA_INDEX_URL",
    "DispatchScadaCollection",
    "collect_latest_dispatch_scada",
]
