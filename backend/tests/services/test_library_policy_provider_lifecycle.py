from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.dependencies import cleanup, service_providers
from infrastructure.cache.cache_keys import ALBUM_TRACKS_INFO_PREFIX


def test_management_provider_graph_clears_every_transitive_consumer(
    monkeypatch,
) -> None:
    names = (
        "get_artwork_projection_service",
        "get_automatic_import_management_service",
        "get_automatic_scan_management_service",
        "get_download_orchestrator",
        "get_download_service",
        "get_drop_import_service",
        "get_file_processor",
        "get_free_music_service",
        "get_library_management_planner",
        "get_library_management_preview_service",
        "get_library_management_worker",
        "get_target_album_identification_service",
        "get_target_download_orchestrator",
        "get_target_download_service",
        "get_target_drop_import_service",
        "get_target_explicit_reidentification_worker",
        "get_target_file_processor",
        "get_target_free_music_service",
        "get_target_import_library_service",
        "get_target_library_operation_service",
        "get_target_library_operation_supervisor",
        "get_target_library_review_service",
        "get_target_status_service",
    )
    providers: dict[str, MagicMock] = {}
    for name in names:
        provider = MagicMock()
        provider.cache_clear = MagicMock()
        monkeypatch.setattr(service_providers, name, provider)
        providers[name] = provider
    genre = MagicMock()
    genre.cache_clear = MagicMock()
    monkeypatch.setattr(cleanup, "get_genre_projection_service", genre)

    cleanup.clear_library_management_provider_graph()

    for provider in (*providers.values(), genre):
        provider.cache_clear.assert_called_once_with()


def test_target_download_graph_uses_new_naming_template_after_invalidation(
    monkeypatch,
) -> None:
    state = {"template": "{track:02d} {title}.{ext}"}
    monkeypatch.setattr(
        service_providers,
        "get_target_file_processor",
        MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        service_providers, "get_target_library_repository", lambda: object()
    )
    monkeypatch.setattr(service_providers, "get_target_album_service", lambda: object())
    monkeypatch.setattr(service_providers, "get_cache", lambda: object())
    monkeypatch.setattr(service_providers, "get_disk_cache", lambda: object())
    monkeypatch.setattr(
        service_providers,
        "_build_target_import_invalidation",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        service_providers,
        "_build_download_orchestrator",
        lambda **_kwargs: SimpleNamespace(naming_template=state["template"]),
    )
    service_providers.get_target_download_orchestrator.cache_clear()

    existing = service_providers.get_target_download_orchestrator()
    state["template"] = "{track:02d} - {title}.{ext}"
    cleanup.clear_library_policy_dependent_caches()
    replacement = service_providers.get_target_download_orchestrator()

    assert existing.naming_template == "{track:02d} {title}.{ext}"
    assert replacement.naming_template == "{track:02d} - {title}.{ext}"
    assert replacement is not existing

    service_providers.get_target_download_orchestrator.cache_clear()


@pytest.mark.asyncio
async def test_native_track_cache_is_cleared_after_scan_and_import() -> None:
    memory_cache = MagicMock()
    memory_cache.delete = AsyncMock()
    memory_cache.clear_prefix = AsyncMock()
    disk_cache = MagicMock()
    disk_cache.delete_album = AsyncMock()

    scan_invalidation = service_providers._build_scan_invalidation(
        memory_cache, disk_cache
    )
    await scan_invalidation({"release-group-1"})
    memory_cache.delete.assert_any_await(f"{ALBUM_TRACKS_INFO_PREFIX}release-group-1")

    memory_cache.delete.reset_mock()
    import_invalidation = service_providers._build_target_import_invalidation(
        memory_cache, disk_cache
    )
    await import_invalidation(
        SimpleNamespace(musicbrainz_id="release-group-1", artist_mbid=None)
    )
    memory_cache.delete.assert_any_await(f"{ALBUM_TRACKS_INFO_PREFIX}release-group-1")
