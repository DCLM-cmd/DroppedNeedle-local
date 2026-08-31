"""A disc subfolder has to be recognisable as one.

MusicBrainz qualifies a medium format by its physical size, so the naming template
produced ``12" Vinyl 01`` - sanitised on disk to ``12_ Vinyl 01``. Media scanners
detect a disc subfolder by a LEADING format word, so every vinyl release was read as
one album per side: "Ants From Up There" and "Sinner Get Ready" each appeared twice
in Jellyfin, and Jellyfin then wrote an album.nfo into each side, pinning the split.
"""

import pytest

from services.native.library_management_naming_policy import normalise_medium_format


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('12" Vinyl', "Vinyl"),
        ('7" Vinyl', "Vinyl"),
        ('10" Shellac', "Shellac"),
        ('12″ Vinyl', "Vinyl"),
        ("12in Vinyl", "Vinyl"),
        ("7 inch Vinyl", "Vinyl"),
        ('  12" Vinyl  ', "Vinyl"),
    ],
)
def test_a_size_qualifier_is_dropped(raw, expected) -> None:
    assert normalise_medium_format(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["CD", "Digital Media", "Vinyl", "Cassette", "DVD", "SACD", "Blu-ray",
     "Hybrid SACD", "DVD-Audio", "8cm CD"],
)
def test_formats_that_already_lead_with_the_word_are_untouched(raw) -> None:
    assert normalise_medium_format(raw) == raw


def test_an_empty_format_stays_empty() -> None:
    """The template falls back to "Disc" on empty - that must keep working."""
    assert normalise_medium_format("") == ""
    assert normalise_medium_format(None) == ""


def test_the_result_leads_with_the_format_word() -> None:
    """The property that actually matters to a scanner."""
    assert normalise_medium_format('12" Vinyl').lower().startswith("vinyl")
