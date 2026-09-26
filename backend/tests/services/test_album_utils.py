from services.album_utils import (
    EXPLICIT_RANK_CLEAN,
    EXPLICIT_RANK_EXPLICIT,
    EXPLICIT_RANK_UNMARKED,
    explicit_rank,
    extract_tracks,
    get_ranked_releases,
    is_clean_release,
)


def test_extract_tracks_preserves_disc_numbers_and_track_positions():
    release_data = {
        "media": [
            {
                "position": "1",
                "tracks": [
                    {
                        "position": "1",
                        "title": "Disc One Intro",
                        "length": 1000,
                        "recording": {"id": "rec-1", "title": "Disc One Intro"},
                    },
                    {
                        "position": "2",
                        "title": "Disc One Main",
                        "recording": {
                            "id": "rec-2",
                            "title": "Disc One Main",
                            "length": 2000,
                        },
                    },
                ],
            },
            {
                "position": "2",
                "tracks": [
                    {
                        "position": "1",
                        "title": "Disc Two Outro",
                        "length": 3000,
                        "recording": {"id": "rec-3", "title": "Disc Two Outro"},
                    }
                ],
            },
        ]
    }

    tracks, total_length = extract_tracks(release_data)

    assert [
        (track.disc_number, track.position, track.title, track.recording_id)
        for track in tracks
    ] == [
        (1, 1, "Disc One Intro", "rec-1"),
        (1, 2, "Disc One Main", "rec-2"),
        (2, 1, "Disc Two Outro", "rec-3"),
    ]
    assert total_length == 6000


def test_extract_tracks_prefers_exact_release_track_title():
    release_data = {
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "position": 14,
                        "title": "The Fisherman Will Be Bewildered",
                        "recording": {
                            "id": "ec935e35-b2fa-4925-aa83-052d9e3e69f1",
                            "title": "The Fishermen Will Be Bewildered",
                        },
                    }
                ],
            }
        ]
    }

    tracks, _total_length = extract_tracks(release_data)

    assert tracks[0].title == "The Fisherman Will Be Bewildered"


# --- edition ranking: explicit over clean -----------------------------------
# Real case: $uicideboy$ "Long Term Effects of SUFFERING" holds a clean and an
# explicit cut, identical in title, status and country. Nothing but the
# lexicographic MBID tiebreak separated them, and 020fc885 (clean) sorts before
# 4c5084cc (explicit) - so the library was handed the censored record.

_LTEOS = {
    "title": "Long Term Effects of SUFFERING",
    "releases": [
        {
            "id": "4c5084cc-2302-4f0d-93e5-875f343b4219",
            "title": "Long Term Effects of SUFFERING",
            "status": "Official",
            "disambiguation": "explicit",
            "country": "XW",
        },
        {
            "id": "020fc885-d505-4ee6-9bd0-dbe0c0b1cf82",
            "title": "Long Term Effects of SUFFERING",
            "status": "Official",
            "disambiguation": "clean",
            "country": "XW",
        },
        {
            "id": "da707ecd-764a-4082-af00-a0f4b603030f",
            "title": "Long Term Effects of SUFFERING",
            "status": "Official",
            "disambiguation": "",
            "country": "US",
        },
    ],
}


def test_explicit_release_outranks_the_clean_cut_of_the_same_record():
    ranked = get_ranked_releases(_LTEOS)

    assert ranked[0]["disambiguation"] == "explicit"
    assert ranked[-1]["disambiguation"] == "clean"


def test_an_unmarked_release_sits_between_explicit_and_clean():
    """Usually the original cut; demoting it below a known-clean one is worse."""
    ranked = [r["disambiguation"] for r in get_ranked_releases(_LTEOS)]

    assert ranked == ["explicit", "", "clean"]


def test_edition_outranks_format_so_a_censored_digital_cut_never_wins():
    group = {
        "title": "Record",
        "releases": [
            {
                "id": "aaaa1111-0000-0000-0000-000000000000",
                "title": "Record",
                "status": "Official",
                "disambiguation": "clean",
                "country": "XW",
            },
            {
                "id": "bbbb2222-0000-0000-0000-000000000000",
                "title": "Record",
                "status": "Official",
                "disambiguation": "explicit",
                "packaging": "Gatefold Cover",
                "country": "DE",
            },
        ],
    }

    assert get_ranked_releases(group)[0]["disambiguation"] == "explicit"


def test_markers_match_whole_words_only():
    """'explicit' inside prose, or a 'cleaned-up' comment, is not an edition."""
    assert explicit_rank({"disambiguation": "remastered"}) == EXPLICIT_RANK_UNMARKED
    assert explicit_rank({"disambiguation": "uncleaned tape source"}) == (
        EXPLICIT_RANK_UNMARKED
    )
    assert explicit_rank({"disambiguation": "clean"}) == EXPLICIT_RANK_CLEAN
    assert explicit_rank({"title": "Album (Clean Version)"}) == EXPLICIT_RANK_CLEAN
    assert explicit_rank({"disambiguation": "uncensored"}) == EXPLICIT_RANK_EXPLICIT


def test_is_clean_release_names_only_the_censored_cut():
    assert is_clean_release({"disambiguation": "clean"}) is True
    assert is_clean_release({"disambiguation": "explicit"}) is False
    assert is_clean_release({"disambiguation": ""}) is False


from types import SimpleNamespace

from services.album_utils import audio_tracks, is_audio_medium


def test_extract_tracks_stamps_medium_format_per_medium():
    release_data = {
        "media": [
            {
                "position": "1",
                "format": "CD",
                "tracks": [
                    {
                        "position": "1",
                        "title": "Audio Song",
                        "recording": {"id": "rec-1", "title": "Audio Song"},
                    }
                ],
            },
            {
                "position": "2",
                "format": "DVD",
                "tracks": [
                    {
                        "position": "1",
                        "title": "Video Clip",
                        "recording": {"id": "rec-2", "title": "Video Clip"},
                    }
                ],
            },
            {
                "position": "3",
                "tracks": [
                    {
                        "position": "1",
                        "title": "Unknown Carrier",
                        "recording": {"id": "rec-3", "title": "Unknown Carrier"},
                    }
                ],
            },
        ]
    }

    tracks, _total = extract_tracks(release_data)

    assert [track.media_format for track in tracks] == ["CD", "DVD", None]


def test_is_audio_medium_video_carriers_excluded_audio_kept():
    for fmt in ("CD", "DVD-Audio", "SACD", "Vinyl", "Digital Media", "Cassette"):
        assert is_audio_medium(fmt) is True
    for fmt in ("DVD", "DVD-Video", "Blu-ray", "HD-DVD", "VHS", "Video CD", "Laserdisc"):
        assert is_audio_medium(fmt) is False
    assert is_audio_medium("  dvd  ") is False  # case/whitespace tolerant
    # Fail-open: missing or unrecognized formats never strand an acquisition.
    assert is_audio_medium(None) is True
    assert is_audio_medium("") is True
    assert is_audio_medium("Future-Carrier-3000") is True


def test_audio_tracks_filters_video_duck_typed_and_fail_open():
    tracks = [
        SimpleNamespace(title="a", media_format="CD"),
        SimpleNamespace(title="b", media_format="DVD"),
        SimpleNamespace(title="c"),  # legacy shape without the field: kept
        SimpleNamespace(title="d", media_format=None),  # local rows: kept
    ]

    assert [t.title for t in audio_tracks(tracks)] == ["a", "c", "d"]
