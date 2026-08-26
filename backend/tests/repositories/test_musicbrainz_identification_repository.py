import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.exceptions import ExternalServiceError
from infrastructure.queue.priority_queue import RequestPriority
from models.identification import AlbumCandidate
from repositories.musicbrainz_base import select_edition
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
        "get_album_candidate_editions",
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


def test_select_edition_dated_release_beats_year_only_same_year():
    # NEW-DECISION-02 parsed-date ordering: within one year a fully dated
    # release outranks a year-only sibling of equal proximity and status.
    releases = [
        {
            "id": "year-only",
            "status": "Official",
            "date": "2024",
            "media": [{"track-count": 9}],
        },
        {
            "id": "dated",
            "status": "Official",
            "date": "2024-01-31",
            "media": [{"track-count": 11}],
        },
    ]
    assert select_edition(releases, 10) == "dated"


def test_select_edition_prefers_xw_country_on_final_tie():
    releases = [
        {
            "id": "rel-gb",
            "status": "Official",
            "country": "GB",
            "date": "2024-03-03",
            "media": [{"track-count": 10}],
        },
        {
            "id": "rel-xw",
            "status": "Official",
            "country": "XW",
            "date": "2024-03-03",
            "media": [{"track-count": 10}],
        },
    ]
    # Country preference ranks above MBID: rel-gb is lexicographically
    # first but XW wins the tie.
    assert select_edition(releases, 10) == "rel-xw"


def test_select_edition_skips_zero_track_count_even_when_otherwise_best():
    releases = [
        # earliest date + Official, but zero medium data: skipped
        {
            "id": "a-empty-official",
            "status": "Official",
            "date": "1970-01-01",
            "media": [{"track-count": 0}],
        },
        # no media at all: skipped
        {"id": "b-no-media", "status": "Official"},
        {"id": "c-counted", "status": "Promotion", "date": "2020",
         "media": [{"track-count": 10}]},
    ]
    assert select_edition(releases, 10) == "c-counted"


def test_select_edition_mbid_breaks_full_ties_deterministically():
    releases = [
        {
            "id": "zzzz-last",
            "status": "Official",
            "country": "XW",
            "date": "2024-03-03",
            "media": [{"track-count": 10}],
        },
        {
            "id": "aaaa-first",
            "status": "Official",
            "country": "XW",
            "date": "2024-03-03",
            "media": [{"track-count": 10}],
        },
    ]
    assert select_edition(releases, 10) == "aaaa-first"


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


@pytest.mark.asyncio
async def test_get_album_candidate_editions_ranks_siblings_by_recall_key() -> None:
    """Phase 2 sibling trial: two ranked editions of one group come back in
    recall_key order (proximity -> Official -> parsed date -> XW), each as a
    full candidate built from its own fetched release payload."""
    musicbrainz = SimpleNamespace(
        get_release_group_by_id=AsyncMock(
            return_value={
                "id": "rg-siblings",
                "title": "Album",
                "primary-type": "Album",
                "secondary-types": [],
                "artist-credit": [{"name": "Artist", "artist": {"id": "artist-1"}}],
                "releases": [
                    {
                        # same proximity as the true edition, but Promotion
                        # status loses the Official tie-break
                        "id": "rel-promo-near",
                        "status": "Promotion",
                        "date": "1970",
                        "media": [{"track-count": 2}],
                    },
                    {
                        "id": "rel-official-dated",
                        "status": "Official",
                        "date": "1970-01-01",
                        "media": [{"track-count": 2}],
                    },
                    {
                        # zero-count sibling carries no medium data: skipped
                        "id": "rel-zero-count",
                        "status": "Official",
                        "date": "1969-01-01",
                        "media": [{}],
                    },
                ],
            }
        ),
        get_release_by_id=AsyncMock(
            side_effect=[
                {
                    "id": "rel-official-dated",
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
                },
                {
                    "id": "rel-promo-near",
                    "date": "1970",
                    "media": [
                        {
                            "position": 1,
                            "tracks": [
                                {
                                    "position": 1,
                                    "title": "Track",
                                    "length": 181_000,
                                    "recording": {"id": "recording-promo"},
                                }
                            ],
                        }
                    ],
                },
            ]
        ),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    editions = await repository.get_album_candidate_editions(
        "rg-siblings", 2, RequestPriority.BACKGROUND_SYNC
    )

    assert [candidate.release_mbid for candidate in editions] == [
        "rel-official-dated",
        "rel-promo-near",
    ]
    assert all(candidate.release_group_mbid == "rg-siblings" for candidate in editions)
    assert editions[0].tracks[0].recording_mbid == "recording-1"
    assert editions[1].tracks[0].recording_mbid == "recording-promo"
    assert [
        call.args[0] for call in musicbrainz.get_release_by_id.await_args_list
    ] == ["rel-official-dated", "rel-promo-near"]


@pytest.mark.asyncio
async def test_get_album_candidate_editions_caps_fetches_at_max_editions() -> None:
    """max_editions bounds full-release fetches; the top pick matches plain
    single-edition selection, and the default of two stays inside the
    owner-approved sibling-trial budget."""
    releases = [
        {"id": f"rel-{index}", "status": "Official", "media": [{"track-count": index}]}
        for index in range(4)
    ]
    musicbrainz = SimpleNamespace(
        get_release_group_by_id=AsyncMock(
            return_value={
                "id": "rg-capped",
                "title": "Album",
                "releases": releases,
            }
        ),
        get_release_by_id=AsyncMock(
            side_effect=lambda release_id, **_: {
                "id": release_id,
                "media": [],
            }
        ),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    default_two = await repository.get_album_candidate_editions(
        "rg-capped", 1, RequestPriority.BACKGROUND_SYNC
    )
    capped_one = await repository.get_album_candidate_editions(
        "rg-capped", 1, RequestPriority.BACKGROUND_SYNC, max_editions=1
    )

    assert [candidate.release_mbid for candidate in default_two] == [
        "rel-1",
        "rel-2",
    ]
    assert [candidate.release_mbid for candidate in capped_one] == ["rel-1"]
    assert musicbrainz.get_release_by_id.await_count == 3


@pytest.mark.asyncio
async def test_get_album_candidate_editions_tolerant_decode_and_dedupe() -> None:
    """Unfetchable siblings are skipped without failing the batch, duplicate
    group listings collapse, and a canonical id already built is never
    fetched twice."""
    musicbrainz = SimpleNamespace(
        get_release_group_by_id=AsyncMock(
            return_value={
                "id": "rg-tolerant",
                "title": "Album",
                "releases": [
                    {"id": "rel-a", "status": "Official", "media": [{"track-count": 1}]},
                    {"id": "rel-b", "status": "Official", "media": [{"track-count": 1}]},
                    {"id": "rel-a", "status": "Official", "media": [{"track-count": 1}]},
                ],
            }
        ),
        get_release_by_id=AsyncMock(
            side_effect=[None, {"id": "rel-b", "media": []}]
        ),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    editions = await repository.get_album_candidate_editions(
        "rg-tolerant", 1, RequestPriority.BACKGROUND_SYNC, max_editions=3
    )

    assert [candidate.release_mbid for candidate in editions] == ["rel-b"]
    assert musicbrainz.get_release_by_id.await_count == 2

@pytest.mark.asyncio
async def test_get_album_candidate_editions_missing_group_returns_empty() -> None:
    """A release group that resolves to no payload yields [] without any
    full-release fetches - absence, never failure."""
    musicbrainz = SimpleNamespace(
        get_release_group_by_id=AsyncMock(return_value=None),
        get_release_by_id=AsyncMock(),
    )
    repository = MusicBrainzIdentificationRepository(musicbrainz)

    editions = await repository.get_album_candidate_editions(
        "rg-missing", 1, RequestPriority.BACKGROUND_SYNC
    )

    assert editions == []
    musicbrainz.get_release_by_id.assert_not_awaited()
