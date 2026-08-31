"""An unconfigured genre source is OFF, not deferred.

"Deferred" means "temporarily unavailable, worth another try". A provider with no
API key will never resolve on a retry, so counting it as deferred put
OPTIONAL_ENRICHMENT_DEFERRED on every track of every Organizer run - a warning the
operator could never clear, which buried the genuine ones. Disabled lyrics already
behave this way (``status="disabled"`` is not in the planner's deferral trigger set);
genres now match.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.exceptions import ConfigurationError, ExternalServiceError
from services.native.genre_projection_service import GenreProjectionService


def _settings(sources):
    return MagicMock(
        sources=sources,
        mode="replace",
        listenbrainz_minimum_count=0,
        listenbrainz_curated_only=False,
        lastfm_minimum_weight=0,
        maximum_count=10,
        enabled=True,
    )


def _canonical():
    canonical = MagicMock()
    canonical.identifiers.release_group_mbid = "rg-1"
    canonical.title = "Post"
    canonical.artist_credits = [MagicMock(display_name="Björk", join_phrase="")]
    return canonical


def _service(*, listenbrainz=None, lastfm=None):
    normalizer = MagicMock()
    normalizer.normalize.return_value = None
    return GenreProjectionService(normalizer, listenbrainz=listenbrainz, lastfm=lastfm)


async def _project(service, sources):
    return await service.project(
        settings=_settings(sources),
        canonical_release=_canonical(),
        existing_genres=(),
    )


@pytest.mark.asyncio
async def test_missing_lastfm_api_key_is_not_a_deferral() -> None:
    """The exact production case: Last.fm enabled as a source, no key configured."""
    lastfm = AsyncMock()
    lastfm.get_album_top_genres.side_effect = ConfigurationError(
        "Last.fm API key is not configured"
    )
    lastfm.get_artist_top_genres.side_effect = ConfigurationError(
        "Last.fm API key is not configured"
    )

    projection = await _project(_service(lastfm=lastfm), ["musicbrainz", "lastfm"])

    assert "lastfm" not in projection.deferred_sources


@pytest.mark.asyncio
async def test_missing_listenbrainz_configuration_is_not_a_deferral() -> None:
    listenbrainz = AsyncMock()
    listenbrainz.get_release_group_genres_batch.side_effect = ConfigurationError(
        "not configured"
    )

    projection = await _project(
        _service(listenbrainz=listenbrainz), ["musicbrainz", "listenbrainz"]
    )

    assert "listenbrainz" not in projection.deferred_sources


@pytest.mark.asyncio
async def test_a_source_that_is_not_wired_at_all_is_not_a_deferral() -> None:
    projection = await _project(_service(), ["musicbrainz", "listenbrainz", "lastfm"])

    assert projection.deferred_sources == ()


@pytest.mark.asyncio
async def test_a_genuinely_transient_failure_still_defers() -> None:
    """The distinction has to keep working: an outage IS worth retrying, and the
    operator should still see that this run's genres are incomplete."""
    listenbrainz = AsyncMock()
    listenbrainz.get_release_group_genres_batch.side_effect = ExternalServiceError(
        "ListenBrainz is down"
    )

    projection = await _project(
        _service(listenbrainz=listenbrainz), ["musicbrainz", "listenbrainz"]
    )

    assert "listenbrainz" in projection.deferred_sources


@pytest.mark.asyncio
async def test_lastfm_outage_still_defers() -> None:
    lastfm = AsyncMock()
    lastfm.get_album_top_genres.side_effect = ExternalServiceError("last.fm is down")
    lastfm.get_artist_top_genres.side_effect = ExternalServiceError("last.fm is down")

    projection = await _project(_service(lastfm=lastfm), ["musicbrainz", "lastfm"])

    assert "lastfm" in projection.deferred_sources


@pytest.mark.asyncio
async def test_an_open_circuit_breaker_defers_instead_of_killing_the_import() -> None:
    """An open breaker means the same thing as an outage - the provider is not
    answering - but it was not caught, so it escaped the projection and aborted the
    whole publication. While ListenBrainz was refusing this installation's address,
    sixteen tracks of an album could not be published because their GENRES could not
    be read. Genres are decorative; the music is not.
    """
    from infrastructure.resilience.retry import CircuitOpenError

    listenbrainz = AsyncMock()
    listenbrainz.get_release_group_genres_batch.side_effect = CircuitOpenError(
        "Circuit breaker 'listenbrainz' is OPEN"
    )

    projection = await _project(
        _service(listenbrainz=listenbrainz), ["musicbrainz", "listenbrainz"]
    )

    assert "listenbrainz" in projection.deferred_sources


@pytest.mark.asyncio
async def test_an_open_lastfm_breaker_defers_too() -> None:
    from infrastructure.resilience.retry import CircuitOpenError

    lastfm = AsyncMock()
    lastfm.get_album_top_genres.side_effect = CircuitOpenError("OPEN")
    lastfm.get_artist_top_genres.side_effect = CircuitOpenError("OPEN")

    projection = await _project(_service(lastfm=lastfm), ["musicbrainz", "lastfm"])

    assert "lastfm" in projection.deferred_sources
