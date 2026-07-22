from services.album_utils import extract_tracks, find_primary_release, get_ranked_releases


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
                        "recording": {"id": "rec-2", "title": "Disc One Main", "length": 2000},
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

    assert [(track.disc_number, track.position, track.title, track.recording_id) for track in tracks] == [
        (1, 1, "Disc One Intro", "rec-1"),
        (1, 2, "Disc One Main", "rec-2"),
        (2, 1, "Disc Two Outro", "rec-3"),
    ]
    assert total_length == 6000


def test_get_ranked_releases_prefers_title_matching_the_release_group():
    """Two releases in one release group with unrelated tracklists (e.g. a "Side A" /
    "Side B" pair released separately under one EP) and identical country/packaging
    must not be resolved by comparing MBIDs lexicographically - the release whose
    title matches the release group's own title (the "whole" release) must win over
    a suffixed partial one, regardless of which MBID sorts first."""
    release_group = {
        "title": "morgen werde ich mich dafür hassen",
        "releases": [
            {
                "id": "53263f4f-b0c2-4faa-ae51-c13269998d1d",  # sorts first lexicographically
                "title": "morgen werde ich mich dafür hassen (side b)",
                "status": "Official",
                "country": "XW",
            },
            {
                "id": "8feb9fef-fad0-4271-9d2b-d9acca84b167",
                "title": "morgen werde ich mich dafür hassen",
                "status": "Official",
                "country": "XW",
            },
        ],
    }

    ranked = get_ranked_releases(release_group)

    assert ranked[0]["id"] == "8feb9fef-fad0-4271-9d2b-d9acca84b167"
    assert find_primary_release(release_group)["id"] == "8feb9fef-fad0-4271-9d2b-d9acca84b167"


def test_get_ranked_releases_falls_back_to_format_rank_when_no_title_matches():
    """When no release's title matches the release group's own title (e.g. every
    release is a themed/edition variant), the existing digital/mainstream-first
    ranking still applies unchanged."""
    release_group = {
        "title": "Album",
        "releases": [
            {
                "id": "vinyl-rel", "title": "Album (Vinyl Edition)", "status": "Official",
                "country": "US", "packaging": "Gatefold",
            },
            {
                "id": "digital-rel", "title": "Album (Digital Edition)", "status": "Official",
                "country": "XW",
            },
        ],
    }

    ranked = get_ranked_releases(release_group)

    assert ranked[0]["id"] == "digital-rel"
