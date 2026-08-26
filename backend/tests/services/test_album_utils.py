from services.album_utils import extract_tracks


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
