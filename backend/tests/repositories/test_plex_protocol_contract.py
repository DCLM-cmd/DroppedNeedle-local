"""Plex protocol conformance contract tests (F-01).

Mirrors ``test_download_client_protocol_contract.py``: ``Protocol`` structural
conformance never checks signatures, so this pins every
``PlexRepositoryProtocol`` method's signature (incl. return annotation) and
async-ness against ``PlexRepository``. It additionally pins the AGENTS.md rule
that protocol modules and implementations must NOT use
``from __future__ import annotations``: annotations must be real objects, so
signature comparison is meaningful instead of string-vs-string.
"""

import inspect
from unittest.mock import AsyncMock

import httpx

from repositories.plex_repository import PlexRepository
from repositories.protocols.plex import PlexRepositoryProtocol

_PLEX_METHODS = (
    "is_configured",
    "configure",
    "ping",
    "get_libraries",
    "get_music_libraries",
    "get_artists",
    "get_albums",
    "get_track_count",
    "get_artist_count",
    "get_album_tracks",
    "get_album_metadata",
    "get_recently_added",
    "get_recently_viewed",
    "get_playlists",
    "get_playlist_items",
    "search",
    "get_genres",
    "get_moods",
    "get_hubs",
    "scrobble",
    "now_playing",
    "build_stream_url",
    "proxy_head_stream",
    "proxy_get_stream",
    "proxy_thumb",
    "proxy_playlist_composite",
    "validate_connection",
    "create_oauth_pin",
    "poll_oauth_pin",
    "clear_cache",
    "configure_cache_ttls",
    "get_sessions",
    "get_listening_history",
)


def _make_repo() -> PlexRepository:
    client = AsyncMock(spec=httpx.AsyncClient)
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    cache.clear_prefix = AsyncMock(return_value=0)
    return PlexRepository(http_client=client, cache=cache)


def _assert_real_annotations(fn, owner: str) -> None:
    for name, param in inspect.signature(fn).parameters.items():
        assert not isinstance(param.annotation, str), f"{owner}.{name}"
    assert not isinstance(inspect.signature(fn).return_annotation, str), owner


def test_impl_conforms_to_plex_protocol():
    impl = _make_repo()

    for name in _PLEX_METHODS:
        proto_fn = getattr(PlexRepositoryProtocol, name)
        impl_fn = getattr(type(impl), name)
        assert inspect.signature(impl_fn) == inspect.signature(proto_fn), name
        assert inspect.iscoroutinefunction(impl_fn) == inspect.iscoroutinefunction(
            proto_fn
        ), name

    assert isinstance(
        inspect.getattr_static(type(impl), "stats_ttl"), property
    )


def test_protocol_and_impl_annotations_are_real_objects():
    for name in _PLEX_METHODS:
        _assert_real_annotations(
            getattr(PlexRepositoryProtocol, name), f"PlexRepositoryProtocol.{name}"
        )
        _assert_real_annotations(
            getattr(PlexRepository, name), f"PlexRepository.{name}"
        )
