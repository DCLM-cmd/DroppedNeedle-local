"""Resolving a MusicBrainz release group to a local album.

Several rows can claim the same release group: a folder rename leaves the old row
behind, an abandoned import leaves an empty shell, a removed album keeps its rows with
every track marked ``missing``. Treating that as an ambiguity made an ABSENT album
unusable - "Couldn't load the track list" on the album page, and "the exact MusicBrainz
edition could not be verified, no download was started" when requesting it. The one
action that would have fixed it, downloading the album, was the action being blocked.

A real ambiguity is two albums that both hold playable files.
"""

import sqlite3

import pytest

from core.exceptions import ConflictError
from infrastructure.persistence.native_library_store import NativeLibraryStore

RG = "ec879320-d2d0-4331-a9ca-fb3fed5c59a6"


@pytest.fixture()
def connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE local_albums (
            id TEXT PRIMARY KEY, title TEXT, retired_into_album_id TEXT
        );
        CREATE TABLE local_album_aliases (alias TEXT, local_album_id TEXT);
        CREATE TABLE local_album_external_identities (
            local_album_id TEXT, provider TEXT, release_group_mbid TEXT
        );
        CREATE TABLE local_tracks (
            id TEXT PRIMARY KEY, local_album_id TEXT, availability TEXT
        );
        """
    )
    return conn


def _album(conn, album_id, title, *, tracks=()):
    conn.execute(
        "INSERT INTO local_albums (id, title, retired_into_album_id) VALUES (?, ?, NULL)",
        (album_id, title),
    )
    conn.execute(
        "INSERT INTO local_album_external_identities "
        "(local_album_id, provider, release_group_mbid) VALUES (?, 'musicbrainz', ?)",
        (album_id, RG),
    )
    for index, availability in enumerate(tracks):
        conn.execute(
            "INSERT INTO local_tracks (id, local_album_id, availability) VALUES (?, ?, ?)",
            (f"{album_id}-{index}", album_id, availability),
        )


def _resolve(conn):
    return NativeLibraryStore._resolve_target_album_pin_id(conn, RG)


def test_one_album_with_files_resolves(connection) -> None:
    _album(connection, "a", "Mann beisst Hund", tracks=["indexed", "indexed"])

    assert _resolve(connection) == "a"


def test_the_album_that_still_has_files_wins_over_leftovers(connection) -> None:
    """A renamed folder leaves the old row behind with everything missing."""
    _album(connection, "old", "Mann beisst Hund", tracks=["missing", "missing"])
    _album(connection, "new", "Mann beisst Hund", tracks=["indexed", "indexed"])

    assert _resolve(connection) == "new"


def test_two_albums_that_both_have_files_are_a_real_ambiguity(connection) -> None:
    """The case the conflict exists for: a release and its deluxe edition, both
    present. Only the user can say which one a pin belongs to."""
    _album(connection, "standard", "OK Computer", tracks=["indexed"])
    _album(connection, "oknotok", "OK Computer: OKNOTOK", tracks=["indexed"])

    with pytest.raises(ConflictError):
        _resolve(connection)


def test_leftover_rows_alone_mean_the_album_is_not_in_the_library(connection) -> None:
    """The live regression, exactly: four rows for OG Keemo's "Mann beisst Hund" -
    the album, its instrumental tape, and two empty shells - none holding a file."""
    _album(connection, "album", "Mann beisst Hund", tracks=["missing"] * 4)
    _album(connection, "instrumentals", "Mann Beisst Hund Instrumental Tape",
           tracks=["missing"] * 4)
    _album(connection, "empty-1", "Mann beisst Hund")
    _album(connection, "empty-2", "Mann Beisst Hund Instrumental Tape")

    assert _resolve(connection) is None


def test_two_empty_shells_alone_are_not_a_conflict(connection) -> None:
    _album(connection, "empty-1", "Fieber")
    _album(connection, "empty-2", "Fieber")

    assert _resolve(connection) is None


def test_a_single_dormant_row_still_resolves(connection) -> None:
    """One row, nothing present: it is still unambiguously that album, so a pin
    written against it keeps working."""
    _album(connection, "only", "Fieber", tracks=["missing"])

    assert _resolve(connection) == "only"


def test_an_unknown_release_group_resolves_to_nothing(connection) -> None:
    assert _resolve(connection) is None


def test_an_excluded_track_does_not_count_as_present(connection) -> None:
    """'excluded' is a file the user removed from the library on purpose."""
    _album(connection, "excluded-only", "Fieber", tracks=["excluded"])
    _album(connection, "also-gone", "Fieber", tracks=["missing"])

    assert _resolve(connection) is None
