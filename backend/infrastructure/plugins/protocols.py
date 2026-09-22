"""Capability protocols - what a plugin implements, and what the host hands it.

A plugin's entrypoint class is instantiated once as ``Entry(context)`` and may
implement any subset of the capability methods matching its manifest. All
methods are async and best-effort: an exception is logged against the plugin
and never propagates into the host flow that triggered it.

Trust and isolation: plugin code runs in-process and is trusted once an admin
enables it. A capability that raises is logged against the plugin and never
propagates into the host flow that triggered it. The plugin context carries
no ambient authority beyond what the host hands it (settings, shared HTTP
client); anything else a plugin wants goes over the authenticated public REST
API like any external client.

Acquisition caps: capabilities that search a source or fetch files stay inert
until an admin explicitly enables the plugin, and each such capability
documents its own bounds (result caps, fetch size limits, licence handling).
The host never presents plugin results as its own curated sources.
"""

import logging
from typing import Callable, Mapping, Protocol, runtime_checkable

import httpx

from infrastructure.msgspec_fastapi import AppStruct
from repositories.protocols.download_client import (
    DownloadClientProtocol,
    DownloadFileRef,
    DownloadMaterialization,
    DownloadSearchResult,
    DownloadTaskStatus,
    EnqueueRequest,
    MountDiagnosis,
    TaskHandle,
)
from repositories.protocols.indexer import (
    IndexerProtocol,
    IndexerResult,
    PluginSearchResult,
    UsenetRelease,
)


class ScrobbleEvent(AppStruct):
    """A play accepted by the scrobble pipeline (already deduped)."""

    artist: str
    track: str
    album: str | None = None
    timestamp: int = 0
    duration_ms: int | None = None
    recording_mbid: str | None = None


class PluginPurchaseLink(AppStruct):
    """A purchase link a plugin contributes to the Where-to-buy section."""

    label: str
    url: str
    kind: str = "digital"  # 'digital' | 'physical' | 'free'


class _UnwiredPublishResult:
    """``ok=False`` fallback when the host did not bind a publish callable.

    Awaitable like the host's ``PluginPublishResult`` so ``await ctx.publish(...)``
    works with and without host wiring; never raises.
    """

    ok = False
    status = 404
    retry_after = 0
    error = "plugin host did not provide a publish callable"

    def __await__(self):  # type: ignore[no-untyped-def]
        async def _self():  # noqa: ANN202
            return self

        return _self().__await__()


class PluginContext:
    """Host-provided services for one plugin instance.

    ``settings`` is a live callable so an admin's settings save applies without
    a reload. ``http`` is a shared factory client (owns timeouts + the app
    User-Agent); plugins must not build their own clients. ``publish`` is the
    host-bound publisher-ingress callable ``(kind, payload=None, *,
    principal="", causation_id=None)`` stamped with this plugin's name: it
    returns the host's publish result and never raises for capability reasons
    (a plugin without the ``publisher`` capability gets an ``ok=False``
    404-style result, never an exception).
    """

    def __init__(
        self,
        *,
        plugin_name: str,
        settings: Callable[[], Mapping[str, str]],
        http: httpx.AsyncClient,
        publish: Callable[..., object] | None = None,
    ) -> None:
        self.plugin_name = plugin_name
        self._settings = settings
        self.http = http
        self._publish = publish
        self.logger = logging.getLogger(f"plugin.{plugin_name}")

    @property
    def settings(self) -> Mapping[str, str]:
        return self._settings()

    def publish(
        self, kind: str, payload: object = None, **kwargs: object
    ) -> object:
        """Publish one allowlisted hint (``indexer_invalidate``/``download_note``/
        ``plugin_notice``) via the host. Requires the ``publisher`` capability;
        without host wiring (never in production: the host always binds it)
        this returns an ``ok=False`` 404-style result, never an exception.
        The host result is awaitable, so plugins may ``await`` this call."""
        if self._publish is None:
            return _UnwiredPublishResult()
        return self._publish(kind, payload, **kwargs)


@runtime_checkable
class ScrobblerCapability(Protocol):
    async def on_scrobble(self, event: ScrobbleEvent) -> None: ...


@runtime_checkable
class PurchaseLinksCapability(Protocol):
    async def purchase_links(
        self, artist: str, album: str, release_group_mbid: str
    ) -> list[PluginPurchaseLink]: ...


class PluginEvent(AppStruct):
    """One fan-out event for subscriber plugins (v1 events API).

    ``kind`` is a closed set; each kind carries exactly one payload struct:
    ``scrobble`` -> :class:`ScrobbleEvent` (also reaches v0 scrobbler plugins),
    ``download_started`` / ``download_completed`` / ``download_failed`` ->
    :class:`DownloadTaskEvent`, ``request_created`` / ``request_fulfilled`` ->
    :class:`RequestEvent`, ``import_finished`` -> :class:`ImportEvent`,
    ``playback_started`` -> :class:`PlaybackEvent`. Only the struct is passed -
    the plugin cannot reach the task row or mutate engine state."""

    kind: str  # "scrobble" | "download_started" | "download_completed" |
    # "download_failed" | "request_created" | "request_fulfilled" |
    # "import_finished" | "playback_started"
    payload: object  # kind-specific AppStruct (see above); one struct per kind
    # Top-level causation id (uuid v4, stamped by the host publisher path) so fan-out
    # can dedup on (causation_id, subscriber). Empty = unstamped (tests/direct builds).
    causation_id: str = ""


class DownloadTaskEvent(AppStruct):
    """Payload for the ``download_started`` / ``download_completed`` /
    ``download_failed`` kinds. ``outcome`` names the terminal state
    (``"started"`` while running, e.g. ``"completed"`` / ``"failed"`` after)."""

    task_id: str
    user_id: str = ""
    release_group_mbid: str = ""
    source: str = ""
    outcome: str = ""


class RequestEvent(AppStruct):
    """Payload for the ``request_created`` / ``request_fulfilled`` kinds."""

    request_id: str
    user_id: str = ""
    release_group_mbid: str = ""
    status: str = ""


class ImportEvent(AppStruct):
    """Payload for the ``import_finished`` kind."""

    release_group_mbid: str
    track_count: int = 0
    source: str = ""


class PlaybackEvent(AppStruct):
    """Payload for the ``playback_started`` kind."""

    artist: str
    track: str
    album: str | None = None
    user_id: str = ""


class IndexerInvalidate(AppStruct):
    """Publisher hint asking the engine to rescout a source. The engine decides;
    the plugin cannot force a rescan."""

    target_source: str


class DownloadNote(AppStruct):
    """Publisher annotation on a task. Annotation only - never a status mutation;
    the host only accepts notes for the publishing plugin's own tasks."""

    task_id: str
    note: str = ""


class PluginNotice(AppStruct):
    """Opaque publisher broadcast, fanned out to subscribers. Never mutates."""

    title: str
    body: str = ""


@runtime_checkable
class SubscriberCapability(Protocol):
    """Consume-only event sink. Dispatch is fire-and-forget with per-plugin
    isolation; a raising subscriber is logged, never propagated."""

    async def on_event(self, event: PluginEvent) -> None: ...


@runtime_checkable
class PublisherCapability(Protocol):
    """Marker for plugins allowed to call ``ctx.publish(kind, payload)``.

    Publishing is a host-bound API, not a plugin method, so there is nothing to
    implement: declaring the capability id admits the plugin to the allowlisted
    publish kinds (``indexer_invalidate``, ``download_note``, ``plugin_notice``).
    """


@runtime_checkable
class SchedulerCapability(Protocol):
    """Periodic tick. No args, no return surface: the tick does its work through
    ``context.http`` (shared factory client) and ``context.settings`` ONLY;
    publishing via ``ctx.publish`` (needs the ``publisher`` capability too,
    else the publish result is ``ok=False``); library file mutations ONLY
    through ``services.plugin_sources.plugin_write_library_file`` and
    plugin-dir state ONLY through the host state-file helpers - never direct
    library writes, never SQLite in v1, no engine handles passed. The host
    runs each tick under ``asyncio.timeout(interval)`` on a TaskRegistry loop
    (``plugin-tick:<name>``): keep ticks short, run blocking work in
    ``asyncio.to_thread``, never block the event loop."""

    async def on_tick(self) -> None: ...


class PluginAlbumEnrichment(AppStruct):
    """Partial album metadata from a plugin. All fields defaulted - partial
    enrichment is normal; anything present first-party wins, never overwritten."""

    biography: str | None = None
    links: list[str] = []
    tags: list[str] = []
    image_urls: list[str] = []


class PluginArtistEnrichment(AppStruct):
    """Partial artist metadata from a plugin. Same merge rule as the album side."""

    biography: str | None = None
    links: list[str] = []
    tags: list[str] = []
    image_urls: list[str] = []


@runtime_checkable
class MetadataProviderCapability(Protocol):
    """Metadata enrichment, consumed below first-party sources. ``None`` means
    "not mine" - the merge treats it as a gap. Failures degrade to ``None``."""

    async def enrich_album(
        self,
        *,
        artist_name: str,
        album_title: str,
        mbid: str | None = None,
        timeout: float = 30.0,
    ) -> PluginAlbumEnrichment | None: ...
    async def enrich_artist(
        self,
        *,
        artist_name: str,
        mbid: str | None = None,
        timeout: float = 30.0,
    ) -> PluginArtistEnrichment | None: ...


class PluginStreamRef(AppStruct):
    """What a streaming_source plugin returns. Exactly one of ``path``/``url`` set.

    ``url`` MAY carry transcode hints (owner override 2026-09-05); the host proxy
    honours them rather than treating every url as opaque passthrough."""

    path: str = ""  # local file the host may serve/transcode
    url: str = ""  # remote http(s) the host proxies (egress-allowlisted)
    content_type: str = ""  # hint; host sniffs when empty
    duration_seconds: float | None = None


@runtime_checkable
class StreamingSourceCapability(Protocol):
    """Byte source fallback, consulted after the local library. ``None`` means
    "not mine" and the router falls through. Args are plain strings (already-
    authed user id) - never the user record, never the token."""

    async def resolve_stream(
        self, recording_mbid: str, user_id: str
    ) -> PluginStreamRef | None: ...


class PluginRouteRequest(AppStruct):
    """Typed transport for one ``/ext/`` call into a plugin. The host builds it
    from the HTTP request, then invokes the plugin handler shape
    ``async def handle_route(method, subpath, query: dict, body: object) ->
    PluginRouteResponse``. ``method`` is GET, POST, or DELETE in v1."""

    method: str
    subpath: str
    query: dict[str, str] = {}
    body: object = None


class PluginRouteResponse(AppStruct):
    """Plugin answer to one ``/ext/`` call. The host clamps ``status`` to 200-299
    plus explicit 400/404 and caps body size; a raising plugin gets the generic
    5xx envelope, never a traceback."""

    status: int
    body: object = None


class PluginScoringHelper:
    """Host-owned scoring surface a plugin MAY use to produce its result scores.

    The plugin's own score is always trusted; these helpers exist so a plugin that
    does not want to write its own matcher can reuse DroppedNeedle's token-matching
    pipeline (same rapidfuzz token_set_ratio + artist-evidence the preflight scorer
    uses). Pure functions of strings - no engine state, no policy gates; the app
    still applies policy to whatever score the plugin finally reports.

    The ``scoring_core`` import stays inside the methods: it lives in the services
    layer, and a top-level import here (infrastructure) would invert the
    Routes -> Services -> Repos/Stores -> Infra layering and risk import cycles
    (services modules reach back into plugin/host pieces). Both methods return a
    0..1 confidence float."""

    def album_match(self, artist: str, album: str, candidate_title: str) -> float:
        from services.native.acquisition.scoring_core import album_match as _score

        return _score(artist, album, candidate_title)

    def track_match(self, artist: str, track: str, filename: str) -> float:
        from services.native.acquisition.scoring_core import track_match as _score

        return _score(artist, track, filename)


CAPABILITY_PROTOCOLS: dict[str, type] = {
    "scrobbler": ScrobblerCapability,
    "purchase_links": PurchaseLinksCapability,
    "download_client": DownloadClientProtocol,
    "indexer": IndexerProtocol,
    "subscriber": SubscriberCapability,
    "publisher": PublisherCapability,
    "metadata_provider": MetadataProviderCapability,
    "scheduler": SchedulerCapability,
    "streaming_source": StreamingSourceCapability,
}
