"""One latest Dispatch SCADA collection cycle."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta, timezone

from .dispatch_scada import parse_dispatch_scada_csv
from .nemweb_dispatch_scada import (
    DispatchScadaArtifact,
    DispatchScadaArtifactRef,
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


def discover_current_dispatch_scada(
    *,
    fetch: Callable[..., NemwebHttpResource] = fetch_nemweb_resource,
) -> tuple[DispatchScadaArtifactRef, ...]:
    """Fetch and discover every canonical CURRENT SCADA reference."""

    index_resource = fetch(
        DISPATCH_SCADA_INDEX_URL,
        max_bytes=DISPATCH_SCADA_INDEX_MAX_BYTES,
    )
    return discover_dispatch_scada_artifacts(
        index_resource.body.decode("utf-8"),
        index_url=DISPATCH_SCADA_INDEX_URL,
    )


def collect_dispatch_scada_artifact(
    reference: DispatchScadaArtifactRef,
    ingestion_version: int,
    correction_version: int = 0,
    *,
    fetch: Callable[..., NemwebHttpResource] = fetch_nemweb_resource,
) -> DispatchScadaCollection:
    """Fetch, verify and parse one canonical CURRENT SCADA artifact."""

    artifact_resource = fetch(
        reference.url,
        max_bytes=DISPATCH_SCADA_ARTIFACT_MAX_BYTES,
    )
    artifact = extract_dispatch_scada_zip(reference, artifact_resource.body)
    records = parse_dispatch_scada_csv(
        artifact.csv_payload,
        source_artifact_id=artifact.reference.source_artifact_id,
        ingestion_version=ingestion_version,
        correction_version=correction_version,
        naive_timezone=timezone(timedelta(hours=10)),
    )
    return DispatchScadaCollection(artifact=artifact, records=records)


def collect_current_dispatch_scada(
    ingestion_version: int,
    correction_version: int = 0,
    *,
    fetch: Callable[..., NemwebHttpResource] = fetch_nemweb_resource,
) -> tuple[DispatchScadaCollection, ...]:
    """Fetch and parse every canonical CURRENT SCADA artifact in order."""

    references = discover_current_dispatch_scada(fetch=fetch)
    return tuple(
        collect_dispatch_scada_artifact(
            reference,
            ingestion_version,
            correction_version,
            fetch=fetch,
        )
        for reference in references
    )


def collect_latest_dispatch_scada(
    ingestion_version: int,
    correction_version: int = 0,
    *,
    fetch: Callable[..., NemwebHttpResource] = fetch_nemweb_resource,
) -> DispatchScadaCollection:
    """Collect the latest canonical Dispatch SCADA artifact."""

    reference = discover_current_dispatch_scada(fetch=fetch)[-1]
    return collect_dispatch_scada_artifact(
        reference,
        ingestion_version,
        correction_version,
        fetch=fetch,
    )


__all__ = [
    "DISPATCH_SCADA_ARTIFACT_MAX_BYTES",
    "DISPATCH_SCADA_INDEX_MAX_BYTES",
    "DISPATCH_SCADA_INDEX_URL",
    "DispatchScadaCollection",
    "collect_current_dispatch_scada",
    "collect_dispatch_scada_artifact",
    "collect_latest_dispatch_scada",
    "discover_current_dispatch_scada",
]
