import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.exceptions import ExternalServiceError
from infrastructure.queue.priority_queue import RequestPriority
from models.identification import AlbumCandidate
from repositories.musicbrainz_identification_repository import (
    MusicBrainzIdentificationRepository,
)
from repositories.protocols.identification import IdentificationProviderProtocol


def test_repository_matches_identification_provider_protocol_signatures() -> None:
    for name in (
        "search_album_candidate_ids",
        "search_release_editions",
        "search_recording_candidate_ids",
        "get_album_candidate",
        "get_exact_release_candidate",
    ):
        assert inspect.signature(
            getattr(IdentificationProviderProtocol, name)
        ) == inspect.signature(getattr(MusicBrainzIdentificationRepository, name))


@pytest.mark.asyncio
async def test_repository_normalizes_provider_payload_and_forwards_priority() -> None:
    musicbrainz = SimpleNamespace(
        search_release_editions=AsyncMock(),
        search_release_groups=AsyncMock(
            return_value=[SimpleNamespace(musicbrainz_id="rg-1")]
        ),
        search_recordings=AsyncMock(
            return_value=[
                SimpleNamespace(
                    release_groups=[SimpleNamespace(release_group_mbid="rg-1")]
                )
            ]
        ),
        get_release_group_by_id=AsyncMock(
            return_value={
                "id": "rg-1",
                "title": "Album",
                "primary-type": "Album",
                "secondary-types": [],
                "artist-credit": [{"name": "Artist", "artist": {"id": "artist-1"}}],
                "releases": [
                    {
                        "id": "release-1",
                        "status": "Official",
                        "date": "2026-01-01",
                        "media": [{"track-count": 1}],
                    }
                ],
            }
        ),
        get_release_by_id=AsyncMock(
            return_value={
                "date": "2026-01-01",
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {
                                "position": 1,
                                "title": "Track",
                                "length": 180_000,
                                "recording": {"id": "recording-1"},
                            }
                        ],
                    }
                ],
            }
        ),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)
    priority = RequestPriority.BACKGROUND_SYNC

    await repository.search_release_editions("Album", "Artist", 12, 24, priority)

    assert await repository.search_album_candidate_ids(
        "Artist", "Album", 8, priority
    ) == ["rg-1"]
    assert await repository.search_recording_candidate_ids(
        "Artist", "Track", 5, priority
    ) == ["rg-1"]
    candidate = await repository.get_album_candidate("rg-1", 1, priority)

    assert isinstance(candidate, AlbumCandidate)
    assert candidate.release_group_mbid == "rg-1"
    assert candidate.tracks[0].recording_mbid == "recording-1"
    assert candidate.tracks[0].duration_seconds == 180
    assert candidate.release_type == "album"
    assert all(
        call.kwargs["priority"] is priority
        for mock in (
            musicbrainz.search_release_groups,
            musicbrainz.search_recordings,
            musicbrainz.get_release_group_by_id,
            musicbrainz.get_release_by_id,
        )
        for call in mock.await_args_list
    )
    musicbrainz.search_release_editions.assert_awaited_once_with(
        "Album", "Artist", limit=12, offset=24, priority=priority
    )


@pytest.mark.asyncio
async def test_exact_release_uses_canonical_provider_ids_and_payload() -> None:
    musicbrainz = SimpleNamespace(
        get_release_by_id=AsyncMock(
            return_value={
                "id": "canonical-release",
                "title": "Album",
                "date": "2020-01-01",
                "artist-credit": [{"name": "Artist", "artist": {"id": "artist-1"}}],
                "release-group": {
                    "id": "rg-1",
                    "title": "Album",
                    "primary-type": "Album",
                },
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {
                                "id": "canonical-release-track",
                                "position": 1,
                                "title": "Track",
                                "recording": {"id": "same-recording"},
                            }
                        ],
                    }
                ],
            }
        ),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    candidate = await repository.get_exact_release_candidate(
        "redirected-release",
        RequestPriority.USER_INITIATED,
    )

    assert candidate is not None
    assert candidate.release_group_mbid == "rg-1"
    assert candidate.release_mbid == "canonical-release"
    assert candidate.tracks[0].release_track_mbid == "canonical-release-track"
    musicbrainz.get_release_by_id.assert_awaited_once_with(
        "redirected-release",
        includes=["recordings", "artist-credits", "release-groups"],
        priority=RequestPriority.USER_INITIATED,
    )


@pytest.mark.asyncio
async def test_exact_release_returns_none_without_a_provider_release() -> None:
    musicbrainz = SimpleNamespace(
        get_release_by_id=AsyncMock(return_value=None),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    candidate = await repository.get_exact_release_candidate(
        "missing-release",
        RequestPriority.USER_INITIATED,
    )

    assert candidate is None
    musicbrainz.get_release_by_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_exact_release_returns_none_without_a_provider_release_group() -> None:
    musicbrainz = SimpleNamespace(
        get_release_by_id=AsyncMock(
            return_value={
                "id": "release-1",
                "title": "Album",
                "artist-credit": [{"name": "Artist", "artist": {"id": "artist-1"}}],
                "media": [],
            }
        ),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    candidate = await repository.get_exact_release_candidate(
        "release-1",
        RequestPriority.USER_INITIATED,
    )

    assert candidate is None


@pytest.mark.asyncio
async def test_exact_release_returns_none_without_a_canonical_provider_id() -> None:
    musicbrainz = SimpleNamespace(
        get_release_by_id=AsyncMock(
            return_value={
                "title": "Album",
                "release-group": {"id": "rg-1"},
                "media": [],
            }
        ),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    candidate = await repository.get_exact_release_candidate(
        "requested-alias", RequestPriority.USER_INITIATED
    )

    assert candidate is None


@pytest.mark.asyncio
async def test_exact_release_propagates_provider_failure_without_substitution() -> None:
    musicbrainz = SimpleNamespace(
        get_release_by_id=AsyncMock(
            side_effect=ExternalServiceError("provider unavailable")
        ),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    with pytest.raises(ExternalServiceError):
        await repository.get_exact_release_candidate(
            "release-1", RequestPriority.USER_INITIATED
        )


# --- F-062: single-source-of-truth edition selection across lanes -------------


def test_select_edition_prefers_official_skips_zero_count_and_ties_stably():
    from repositories.musicbrainz_base import select_edition

    releases = [
        # zero-track-count promo: skipped even though it is listed first
        {"id": "rel-promo", "status": "Official", "media": [{}]},
        # unofficial at the exact target count...
        {
            "id": "rel-unofficial",
            "status": "Promotion",
            "date": "1970-01-01",
            "media": [{"track-count": 3}],
        },
        # ...and an Official sibling equally close: Official wins.
        {
            "id": "rel-official",
            "status": "Official",
            "date": "1970-01-01",
            "media": [{"track-count": 3}],
        },
    ]
    assert select_edition(releases, 3) == "rel-official"
    # all-zero-count input has nothing to rank
    assert (
        select_edition(
            [{"id": "rel-a", "media": [{}]}, {"id": "rel-b", "status": "Official"}],
            5,
        )
        is None
    )
    # closest track count beats everything else
    assert (
        select_edition(
            [
                {"id": "far", "status": "Official", "media": [{"track-count": 30}]},
                {"id": "near", "status": "Promotion", "media": [{"track-count": 4}]},
            ],
            4,
        )
        == "near"
    )


@pytest.mark.asyncio
async def test_get_album_candidate_selects_counted_official_edition() -> None:
    """F-062 convergence (native lane): with a zero-count promo listed first
    and a counted Official edition second, get_album_candidate resolves to
    the SAME edition MBID the folder/drop-import matcher picks."""
    musicbrainz = SimpleNamespace(
        get_release_group_by_id=AsyncMock(
            return_value={
                "id": "rg-converge",
                "title": "Album",
                "primary-type": "Album",
                "secondary-types": [],
                "artist-credit": [{"name": "Artist", "artist": {"id": "artist-1"}}],
                "releases": [
                    {"id": "rel-promo", "status": "Promotion", "media": [{}]},
                    {
                        "id": "rel-official-counted",
                        "status": "Official",
                        "date": "1970-01-01",
                        "media": [{"track-count": 1}],
                    },
                ],
            }
        ),
        get_release_by_id=AsyncMock(
            return_value={
                "date": "1970-01-01",
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {
                                "position": 1,
                                "title": "Track",
                                "length": 180_000,
                                "recording": {"id": "recording-1"},
                            }
                        ],
                    }
                ],
            }
        ),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    candidate = await repository.get_album_candidate(
        "rg-converge", 1, RequestPriority.BACKGROUND_SYNC
    )

    assert isinstance(candidate, AlbumCandidate)
    assert candidate.release_mbid == "rel-official-counted"
    musicbrainz.get_release_by_id.assert_awaited_once()
    assert (
        musicbrainz.get_release_by_id.await_args.args[0] == "rel-official-counted"
    )


@pytest.mark.asyncio
async def test_get_album_candidate_all_zero_count_editions_return_none() -> None:
    """F-062: no ranked edition possible -> honest None instead of guessing."""
    musicbrainz = SimpleNamespace(
        get_release_group_by_id=AsyncMock(
            return_value={
                "id": "rg-empty",
                "title": "Album",
                "releases": [
                    {"id": "rel-zero", "status": "Official", "media": [{}]}
                ],
            }
        ),
        get_release_by_id=AsyncMock(),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    assert (
        await repository.get_album_candidate(
            "rg-empty", 1, RequestPriority.BACKGROUND_SYNC
        )
        is None
    )
    musicbrainz.get_release_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_both_lanes_resolve_same_group_to_same_edition_mbid() -> None:
    """F-062 convergence proof: ONE release-group fixture (zero-count promo
    listed first, counted Official edition second) fed through BOTH the
    native identification lane and the folder/drop-import matcher resolves
    to the identical edition MBID."""
    from services.native.album_matcher import AlbumIdentifier

    releases = [
        {"id": "rel-promo", "status": "Promotion", "media": [{}]},
        {
            "id": "rel-official-counted",
            "status": "Official",
            "date": "1970-01-01",
            "media": [{"track-count": 1}],
        },
    ]
    release_detail = {
        "date": "1970-01-01",
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "position": 1,
                        "title": "Track",
                        "length": 180_000,
                        "recording": {"id": "recording-1"},
                    }
                ],
            }
        ],
    }

    # Lane 1: native identification repository.
    musicbrainz = SimpleNamespace(
        get_release_group_by_id=AsyncMock(
            return_value={
                "id": "rg-shared",
                "title": "Album",
                "primary-type": "Album",
                "secondary-types": [],
                "artist-credit": [{"name": "Artist", "artist": {"id": "artist-1"}}],
                "releases": releases,
            }
        ),
        get_release_by_id=AsyncMock(return_value=release_detail),
    )
    native = MusicBrainzIdentificationRepository(musicbrainz)
    candidate = await native.get_album_candidate(
        "rg-shared", 1, RequestPriority.BACKGROUND_SYNC
    )
    assert candidate is not None

    # Lane 2: folder / drop-import matcher.
    folder_repo = SimpleNamespace(
        get_release_group_by_id=AsyncMock(
            return_value={
                "id": "rg-shared",
                "title": "Album",
                "artist-credit": [{"name": "Artist", "artist": {"id": "artist-1"}}],
                "releases": releases,
            }
        ),
        get_release_by_id=AsyncMock(return_value=release_detail),
    )
    identifier = AlbumIdentifier(folder_repo)
    meta, tracks = await identifier.release_tracks("rg-shared", 1)

    assert candidate.release_mbid == "rel-official-counted"
    assert meta.release_mbid == "rel-official-counted"
    assert [t.recording_mbid for t in tracks] == ["recording-1"]
