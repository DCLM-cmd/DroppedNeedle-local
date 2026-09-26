"""Prowlarr wire models (third-party shapes).

Modelled on the inspected devopsarr/prowlarr-py generated-client docs
(``SearchApi``/``IndexerApi``/``ReleaseResource``, generated from Prowlarr's
OpenAPI), live-verified against Prowlarr **2.3.5.5327** (see
``ProwlarrClient`` docstring for the confirmed shapes). Tolerant defaults so
absent or unknown fields never break decode.
"""

import msgspec


class ProwlarrCategory(msgspec.Struct, rename="camel"):
    id: int = 0
    name: str = ""


class ProwlarrRelease(msgspec.Struct, rename="camel"):
    """One ``ReleaseResource`` from ``/api/v1/search`` (usenet + torrent mixed;
    the indexer filters to ``protocol == "usenet"`` with a usable download URL)."""

    guid: str = ""
    title: str = ""
    size: int = 0
    files: int | None = None
    grabs: int | None = None
    indexer_id: int = 0
    indexer: str = ""
    categories: list[ProwlarrCategory] = []
    download_url: str = ""
    magnet_url: str = ""
    protocol: str = ""  # "usenet" | "torrent"
    publish_date: str = ""  # ISO-8601 when present; "" when absent
    seeders: int | None = None
    leechers: int | None = None


class ProwlarrIndexerInfo(msgspec.Struct, rename="camel"):
    """One ``IndexerResource`` from ``GET /api/v1/indexer`` (subset: identity +
    protocol + enablement for health/count reporting)."""

    id: int = 0
    name: str = ""
    protocol: str = ""  # "usenet" | "torrent"
    enable: bool = False


class ProwlarrSystemStatus(msgspec.Struct, rename="camel"):
    """``GET /api/v1/system/status`` (subset; verified live on 2.3.5.5327).
    Degraded-optional: callers must not hard-fail when it 404s."""

    version: str = ""
