"""A multi-disc release is one album, not one album per disc folder.

The grouping key contains the track's directory. A release stored as
``Album/CD 1`` + ``Album/CD 2`` therefore produced a separate catalog album per disc:
"Ants From Up There" appeared three times, "Sinner Get Ready" three times. The disc is
not lost by collapsing - it is already carried per track as ``disc_number``.
"""

import pytest

from services.native.local_album_grouper import grouping_directory


@pytest.mark.parametrize(
    "directory",
    [
        "Black Country, New Road/Ants From Up There (2022)/12_ Vinyl 01",
        "Lingua Ignota/Sinner Get Ready (2021)/12_ Vinyl 02",
        "Radiohead/A Moon Shaped Pool (2016)/CD1",
        "A/B (2020)/CD 2",
        "A/B (2020)/Disc 1",
        "A/B (2020)/disk 3",
        "A/B (2020)/LP 2",
        "OG Keemo/Fieber (2024)/Digital Media 01",
        "OG Keemo/Fieber (2024)/Digital Media 02",
        "A/B (2020)/Cassette 1",
    ],
)
def test_a_disc_subfolder_groups_into_its_album(directory) -> None:
    assert grouping_directory(directory + "/01.flac") == directory.rsplit("/", 1)[0]


@pytest.mark.parametrize(
    "directory",
    [
        "A/B (2020)",
        "A/Vinyl Edition (2020)",
        "A/B (2020)/Bonus Tracks",
        "A/B (2020)/Instrumentals",
        "A/Live at Disc Golf (2019)",
        "A/CD Singles Collection (1999)",
        "A/Digital Media Collection (2020)",
    ],
)
def test_a_real_album_folder_is_never_collapsed(directory) -> None:
    """The keyword alone is not enough - a disc folder names a disc NUMBER."""
    assert grouping_directory(directory + "/01.flac") == directory


def test_a_top_level_directory_is_left_alone() -> None:
    """Nothing to collapse into; must not walk above the library root."""
    assert grouping_directory("CD 1/01.flac") == "CD 1"


def test_the_two_discs_of_one_release_share_a_key() -> None:
    """The point of the whole thing, stated directly."""
    first = grouping_directory("Artist/Album (2022)/CD 1/01.flac")
    second = grouping_directory("Artist/Album (2022)/CD 2/01.flac")

    assert first == second == "Artist/Album (2022)"
