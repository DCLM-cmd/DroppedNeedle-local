"""Explicit cuts win over censored ones, on every lane that picks a release.

A release group routinely holds a clean and an explicit cut of one record. They
agree on title, status, country, format and - on the wire - on score, so every
ranker fell through to its arbitrary last tie-breaker. For $uicideboy$ "Long Term
Effects of SUFFERING" that tie-breaker was the lexicographic MBID, and the clean
edition (020fc885) sorts before the explicit one (4c5084cc): the library was
handed the censored record with no signal that anything was wrong.
"""

from services.album_utils import get_ranked_releases, is_clean_release
from services.native.album_preflight_scorer import _edition_rank as soulseek_rank
from services.native.edition_suffix import (
    RATING_CLEAN,
    RATING_EXPLICIT,
    RATING_UNMARKED,
    edition_rating,
)
from services.native.newznab_release_scorer import _edition_rank as usenet_rank


def test_the_real_release_group_now_yields_the_explicit_cut():
    group = {
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
        ],
    }

    assert get_ranked_releases(group)[0]["id"].startswith("4c5084cc")


def test_both_source_lanes_agree_on_the_ordering():
    """Soulseek and Usenet must not disagree about which cut is wanted."""
    for rank in (soulseek_rank, usenet_rank):
        assert rank("Artist - Album (Explicit) [FLAC]") == 2
        assert rank("Artist - Album [FLAC]") == 1
        assert rank("Artist - Album (Clean) [FLAC]") == 0


def test_a_clean_scene_release_loses_to_an_unmarked_one():
    assert usenet_rank("Artist-Album-CLEAN-WEB-FLAC-2021-GRP") < usenet_rank(
        "Artist-Album-WEB-FLAC-2021-GRP"
    )


def test_edition_rating_reads_disambiguation_and_title_together():
    assert edition_rating("clean", "Long Term Effects of SUFFERING") == RATING_CLEAN
    assert edition_rating("", "Album (Explicit)") == RATING_EXPLICIT
    assert edition_rating(None, "Album") == RATING_UNMARKED


def test_explicit_wins_when_a_release_carries_both_words():
    """A "clean/explicit" pairing note must not read as the censored cut."""
    assert edition_rating("clean and explicit versions exist") == RATING_EXPLICIT


def test_prose_is_not_an_edition_marker():
    assert edition_rating("uncleaned tape source") == RATING_UNMARKED
    assert edition_rating("remastered") == RATING_UNMARKED
    assert is_clean_release({"disambiguation": "remastered"}) is False
