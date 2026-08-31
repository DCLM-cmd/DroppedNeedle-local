"""Track/tag comparison ignores case and punctuation; credits match on any shared artist.

Raw ``token_set_ratio`` is case- AND punctuation-sensitive, so "THE TITLE" vs "The
Title" and "Don't" vs "Dont" scored as partial mismatches and pushed correct imports
into review. And a downloaded file usually credits every performer where the provider
credits only the primary, which read as a different artist entirely.

Matching is deliberately loose; what gets WRITTEN is not. The Organizer projects
``track.title`` from the provider (see managed_field_registry), so a file that matched
on a punctuation-stripped key is still renamed and tagged with MusicBrainz's spelling.
"""

import pytest

from services.native.title_match import (
    artist_names,
    artists_overlap,
    match_key,
    similarity,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("THE TITLE", "The Title"),
        ("Don't Stop", "Dont Stop"),
        ("Rock & Roll", "Rock and Roll" .replace(" and ", " & ")),
        ("Song (Remastered)", "Song [Remastered]"),
        ("Mötley Crüe", "Motley Crue"),
        ("Sigur Rós - Svefn-g-englar", "sigur ros svefn g englar"),
        ("Dr. Feelgood!", "dr feelgood"),
    ],
)
def test_formatting_differences_do_not_change_identity(left, right) -> None:
    assert similarity(left, right) == 100


def test_genuinely_different_titles_still_differ() -> None:
    assert similarity("Paprika", "Kenan Vs. Kel") < 60


def test_match_key_is_the_comparison_form() -> None:
    assert match_key("Mötley Crüe - Dr. Feelgood!") == "motley crue dr feelgood"


def test_cjk_is_not_mangled() -> None:
    """fold() leaves CJK intact; romanising it would destroy the identity."""
    assert match_key("宇多田ヒカル") == "宇多田ヒカル"


@pytest.mark.parametrize(
    "credit",
    [
        "Artist feat. Guest",
        "Artist ft. Guest",
        "Artist featuring Guest",
        "Artist & Guest",
        "Artist; Guest",
        "Artist, Guest",
        "Artist x Guest",
        "Artist vs. Guest",
    ],
)
def test_any_shared_artist_is_the_same_release(credit) -> None:
    """The provider credits the primary; the file credits everyone. Same release."""
    assert artists_overlap(credit, "Artist")
    assert artists_overlap("Guest", credit)


def test_a_credit_with_no_shared_artist_still_conflicts() -> None:
    assert not artists_overlap("Artist feat. Guest", "Somebody Else Entirely")


def test_overlap_survives_spelling_differences_between_credits() -> None:
    assert artists_overlap("Jay-Z feat. Beyoncé", "Jay Z")


def test_an_unnamed_side_is_not_evidence_of_conflict() -> None:
    """Untagged is missing data; the caller's other signals decide."""
    assert artists_overlap("", "Artist")
    assert artists_overlap("Artist", "   ")


def test_credits_split_into_their_individual_artists() -> None:
    assert artist_names("Denzel Curry; LAZER DIM 700 & Bktherula") == [
        "denzel curry",
        "lazer dim 700",
        "bktherula",
    ]


def test_a_single_artist_credit_is_one_name() -> None:
    assert artist_names("Radiohead") == ["radiohead"]


def test_the_title_written_on_organize_comes_from_the_provider() -> None:
    """The other half of the contract: match loosely, WRITE authoritatively.

    Comparison ignores case and punctuation so a correct file is not held for review,
    but the Organizer must still put MusicBrainz's spelling into the tag and the
    filename - otherwise the library would keep whatever the downloader happened to
    write. ``path=True`` is what carries it into the filename too.
    """
    from services.native.managed_field_registry import get_managed_field

    title = get_managed_field("title")

    assert title is not None
    assert title.canonical_source == "track.title"
    assert title.source_provider == "musicbrainz"
    assert title.participates_in_path_rendering is True
