"""Shipped example plugins as executable fixtures: capabilities + one round trip each."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.v1.schemas.settings import PluginConfig
from infrastructure.plugins.host import PluginHost
from infrastructure.plugins.protocols import PluginContext, PluginEvent
from repositories.protocols.download_client import DownloadFileRef, EnqueueRequest

EXAMPLES = Path(__file__).parent.parent.parent.parent / "examples" / "plugins"

EXPECTED_CAPABILITIES = {
    "http-catalog": ["download_client", "indexer", "scheduler"],
    "local-folder-client": ["download_client"],
    "local-folder-indexer": ["indexer"],
    "events-echo-toy": ["subscriber", "publisher"],
    "metadata-joke-toy": ["metadata_provider"],
    "stream-toy": ["streaming_source"],
}

CATALOG_URL = "https://example-catalog.test/feed.json"
CATALOG_FEED = {
    "albums": [
        {
            "artist": "Example Artist",
            "title": "Example Album",
            "files": [
                {"url": "https://example-catalog.test/files/track01.flac", "filename": "track01.flac", "size": 9},
                {"url": "https://example-catalog.test/files/track02.flac", "filename": "track02.flac", "size": 9},
            ],
        },
        {
            "artist": "Someone Else",
            "title": "Other Album",
            "files": [
                {"url": "https://example-catalog.test/files/other.flac", "filename": "other.flac", "size": 9},
            ],
        },
    ]
}
FILE_BYTES = b"FLACBYTES"


class FakePrefs:
    def __init__(self) -> None:
        self.configs: dict[str, PluginConfig] = {}

    def get_plugin_config(self, name: str) -> PluginConfig:
        return self.configs.get(name, PluginConfig())

    def enable(self, name: str, settings: dict | None = None) -> None:
        self.configs[name] = PluginConfig(enabled=True, settings=settings or {})


def _load_example(name: str, classname: str):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / name / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, classname)


def _host_with(settings_by_plugin: dict[str, dict]) -> tuple[PluginHost, FakePrefs]:
    prefs = FakePrefs()
    for name, settings in settings_by_plugin.items():
        prefs.enable(name, settings)
    host = PluginHost(plugins_dir=EXAMPLES, preferences_service=prefs)
    host.load_all()
    return host, prefs


def _catalog_http(file_bytes: bytes = FILE_BYTES):
    http = AsyncMock()

    async def _get(url: str, timeout: float | None = None):
        if url == CATALOG_URL:
            return SimpleNamespace(status_code=200, json=lambda: CATALOG_FEED, content=b"{}")
        assert url.startswith("https://example-catalog.test/files/"), url
        return SimpleNamespace(status_code=200, json=lambda: {}, content=file_bytes)

    async def _head(url: str, timeout: float | None = None):
        return SimpleNamespace(status_code=200)

    http.get.side_effect = _get
    http.head.side_effect = _head
    return http


def test_all_six_examples_load_with_expected_active_capabilities():
    host, _ = _host_with({name: {} for name in EXPECTED_CAPABILITIES})
    for name, expected in EXPECTED_CAPABILITIES.items():
        plugin = host.get(name)
        assert plugin is not None, name
        assert plugin.enabled and plugin.instance is not None, name
        assert plugin.active_capabilities == expected, name


@pytest.mark.asyncio
async def test_http_catalog_search_then_enqueue_round_trip(tmp_path):
    cls = _load_example("http-catalog", "HttpCatalog")
    context = PluginContext(
        plugin_name="http-catalog",
        settings=lambda: {"catalog_url": CATALOG_URL, "downloads_dir": str(tmp_path)},
        http=_catalog_http(),
    )
    plugin = cls(context)

    results = await plugin.search_album("Example Artist", "Example Album")
    assert len(results) == 1
    assert results[0].source == "plugin:http-catalog"
    assert results[0].plugin.payload == "album-0"
    assert [f.filename for f in results[0].plugin.files] == ["track01.flac", "track02.flac"]

    handle = await plugin.enqueue(
        EnqueueRequest(
            task_id="task-1",
            source="plugin:http-catalog",
            files=list(results[0].plugin.files),
            payload=results[0].plugin.payload,
        )
    )
    status = await plugin.get_status(handle)
    assert status.task_id == "task-1" and status.status == "completed"
    staged = await plugin.list_completed_files(handle)
    assert sorted(p.name for p in staged) == ["track01.flac", "track02.flac"]
    assert staged[0].read_bytes() == FILE_BYTES
    assert await plugin.get_file_path(handle, "track01.flac") == staged[0]
    assert (await plugin.diagnose_downloads_mount()).supported is False


@pytest.mark.asyncio
async def test_http_catalog_search_miss_is_empty_not_an_error():
    cls = _load_example("http-catalog", "HttpCatalog")
    context = PluginContext(
        plugin_name="http-catalog",
        settings=lambda: {"catalog_url": CATALOG_URL, "downloads_dir": "."},
        http=_catalog_http(),
    )
    assert await cls(context).search_album("Nobody", "Nothing") == []


@pytest.mark.asyncio
async def test_local_folder_client_enqueue_lists_and_locates_files(tmp_path):
    srcdir = tmp_path / "source"
    srcdir.mkdir()
    src = srcdir / "song.flac"
    src.write_bytes(b"AUDIO")
    cls = _load_example("local-folder-client", "LocalFolderClient")
    context = PluginContext(
        plugin_name="local-folder-client",
        settings=lambda: {"source_dir": str(srcdir), "downloads_dir": str(tmp_path / "dl")},
        http=AsyncMock(),
    )
    plugin = cls(context)

    handle = await plugin.enqueue(EnqueueRequest(task_id="task-9", source="plugin:local-folder-client", payload=str(src)))
    status = await plugin.get_status(handle)
    assert status.status == "completed" and status.task_id == "task-9"
    completed = await plugin.list_completed_files(handle)
    assert len(completed) == 1 and completed[0].read_bytes() == b"AUDIO"
    assert await plugin.get_file_path(handle, "song.flac") == completed[0]
    assert await plugin.get_file_path(handle, "missing.flac") is None
    assert (await plugin.diagnose_downloads_mount()).supported is False


@pytest.mark.asyncio
async def test_local_folder_indexer_pairs_with_client_search_to_enqueue_verbatim(tmp_path):
    srcdir = tmp_path / "source"
    srcdir.mkdir()
    src = srcdir / "Test Artist - Test Album.flac"
    src.write_bytes(b"PAIR")
    indexer_cls = _load_example("local-folder-indexer", "LocalFolderIndexer")
    indexer = indexer_cls(
        PluginContext(
            plugin_name="local-folder-indexer",
            settings=lambda: {"source_dir": str(srcdir)},
            http=AsyncMock(),
        )
    )
    results = await indexer.search_album("Test Artist", "Test Album")
    match = next(r for r in results if r.plugin.payload == str(src))
    assert match.source == "plugin:local-folder-client"
    assert match.plugin.files == []

    client_cls = _load_example("local-folder-client", "LocalFolderClient")
    client = client_cls(
        PluginContext(
            plugin_name="local-folder-client",
            settings=lambda: {"source_dir": str(srcdir), "downloads_dir": str(tmp_path / "dl")},
            http=AsyncMock(),
        )
    )
    handle = await client.enqueue(
        EnqueueRequest(task_id="pair-1", source="plugin:local-folder-client", payload=match.plugin.payload)
    )
    staged = await client.list_completed_files(handle)
    assert len(staged) == 1 and staged[0].read_bytes() == b"PAIR"


@pytest.mark.asyncio
async def test_events_echo_dispatch_then_route_publishes_owned_note():
    host, _ = _host_with({"events-echo-toy": {}})
    plugin = host.get("events-echo-toy")
    assert plugin is not None and plugin.instance is not None

    await host.dispatch_event(PluginEvent(kind="download_completed", payload={"task_id": "x"}, causation_id="echo-1"))
    await asyncio.sleep(0.3)
    assert plugin.instance._seen == 1

    result = await host.handle_plugin_route("events-echo-toy", "POST", "note", {}, {"note": "hello"})
    assert result.status == 200 and result.body == {"published": True}
    records = host.drain_published()
    assert len(records) == 1
    assert records[0]["kind"] == "download_note"
    assert records[0]["source_plugin"] == "events-echo-toy"
    assert records[0]["payload"] == {"task_id": plugin.instance.OWN_TASK_ID, "note": "hello"}


@pytest.mark.asyncio
async def test_events_echo_self_publish_never_retriggers_a_publish():
    host, _ = _host_with({"events-echo-toy": {}})
    plugin = host.get("events-echo-toy")
    assert plugin is not None and plugin.instance is not None

    await host.handle_plugin_route("events-echo-toy", "POST", "note", {}, {"note": "again"})
    (record,) = host.drain_published()
    seen_before = plugin.instance._seen
    await host.dispatch_event(
        PluginEvent(kind="download_note", payload=record["payload"], causation_id=record["causation_id"])
    )
    await asyncio.sleep(0.3)
    assert plugin.instance._seen == seen_before + 1
    assert host.drain_published() == []


@pytest.mark.asyncio
async def test_events_echo_note_for_foreign_task_is_attributed_without_oracle():
    host, _ = _host_with({"events-echo-toy": {}})
    result = host.publish_from_plugin(
        "events-echo-toy", "download_note", {"task_id": "someone-elses-task", "note": "hi"}
    )
    assert result.ok is True and result.status == 200
    (record,) = host.drain_published()
    assert record["source_plugin"] == "events-echo-toy"
    assert record["payload"]["task_id"] == "someone-elses-task"


@pytest.mark.asyncio
async def test_metadata_joke_enriches_only_test_artist():
    from infrastructure.plugins.protocols import PluginArtistEnrichment

    cls = _load_example("metadata-joke-toy", "MetadataJoke")
    plugin = cls(PluginContext(plugin_name="metadata-joke-toy", settings=lambda: {}, http=AsyncMock()))

    hit = await plugin.enrich_artist(artist_name="Test Artist")
    assert isinstance(hit, PluginArtistEnrichment)
    assert "fictional" in (hit.biography or "")
    assert hit.tags == ["example", "test-fixture"]
    assert await plugin.enrich_artist(artist_name="Someone Else") is None
    assert await plugin.enrich_album(artist_name="Test Artist", album_title="Anything") is None


@pytest.mark.asyncio
async def test_metadata_joke_fills_gaps_only_first_party_wins():
    from infrastructure.plugins.protocols import PluginArtistEnrichment
    from services.search_enrichment_service import _merge_artist_enrichments

    cls = _load_example("metadata-joke-toy", "MetadataJoke")
    plugin = cls(PluginContext(plugin_name="metadata-joke-toy", settings=lambda: {}, http=AsyncMock()))
    joke = await plugin.enrich_artist(artist_name="Test Artist")
    assert joke is not None

    merged = _merge_artist_enrichments(
        PluginArtistEnrichment(biography="Real bio", tags=["real"]), [joke]
    )
    assert merged is not None
    assert merged.biography == "Real bio"
    assert merged.tags == ["real", "example", "test-fixture"]

    gap_filled = _merge_artist_enrichments(None, [joke])
    assert gap_filled is not None and gap_filled.biography == joke.biography


@pytest.mark.asyncio
async def test_stream_toy_resolves_only_the_known_mbid():
    cls = _load_example("stream-toy", "StreamToy")
    plugin = cls(PluginContext(plugin_name="stream-toy", settings=lambda: {}, http=AsyncMock()))

    ref = await plugin.resolve_stream(cls.KNOWN_MBID, "user-1")
    assert ref is not None and ref.path == "fixtures/test-tone.flac" and not ref.url
    assert await plugin.resolve_stream("11111111-1111-4111-8111-111111111111", "user-1") is None


@pytest.mark.asyncio
async def test_stream_service_checks_auth_before_consulting_the_plugin():
    from services.compat.plugin_stream_service import PluginStreamService

    host, _ = _host_with({"stream-toy": {}})
    plugin = host.get("stream-toy")
    assert plugin is not None and plugin.instance is not None
    calls: list[str] = []
    original = plugin.instance.resolve_stream

    async def _counting(mbid: str, user_id: str):
        calls.append(mbid)
        return await original(mbid, user_id)

    plugin.instance.resolve_stream = _counting
    service = PluginStreamService(host)
    try:
        assert await service.resolve(plugin.instance.KNOWN_MBID, "") is None
        assert calls == []
        ref = await service.resolve(plugin.instance.KNOWN_MBID, "user-1")
        assert ref is not None and calls == [plugin.instance.KNOWN_MBID]
    finally:
        plugin.instance.resolve_stream = original


@pytest.mark.asyncio
async def test_stream_path_validation_drops_traversal():
    from services.compat.plugin_stream_service import PluginStreamService, validated_plugin_path

    host, _ = _host_with({"stream-toy": {}})
    service = PluginStreamService(host)
    assert await validated_plugin_path(service, "/etc/passwd") is None
    assert await validated_plugin_path(service, "../../etc/passwd") is None
    assert await validated_plugin_path(service, "") is None
    inside = await validated_plugin_path(service, str(EXAMPLES / "stream-toy" / "plugin.py"))
    assert inside is not None and inside.is_file()
