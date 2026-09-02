"""Shared edition-suffix normalization for album title comparison (F-MATCH-01).

Both ``musicbrainz_matcher`` and the target evidence engine must fold album
titles through this helper so both surfaces compare the same base title. The
suffix set is an existing provider-matching convention; extend it only with a
separately evidenced provider qualifier.
"""

from __future__ import annotations

import re

EDITION_SUFFIXES = re.compile(
    r"\b(deluxe|remastered|remaster|edition|anniversary|special|expanded|"
    r"complete|bonus|acoustic|live|demo|radio edit|extended|instrumental|"
    r"mono|stereo|explicit|clean|version|single|promo)\b",
    re.IGNORECASE,
)
BRACKETS = re.compile(r"[\(\)\[\]{}]")
WHITESPACE = re.compile(r"\s+")

# The one edition distinction that is NOT foldable. Everything else in
# EDITION_SUFFIXES describes the same music packaged differently, so comparison
# strips it; a censored cut is different music, and stripping it is what let a
# clean record answer a request for the explicit one.
#
# Detection lives beside the stripper on purpose: this file is the declared
# source of truth for edition vocabulary, and the same words already drifted
# across three copies. Callers that need the distinction consume these rather
# than growing a fourth list.
_CLEAN_MARKERS = re.compile(r"\b(clean|censored|edited)\b", re.IGNORECASE)
_EXPLICIT_MARKERS = re.compile(r"\b(explicit|uncensored)\b", re.IGNORECASE)

RATING_EXPLICIT = "explicit"
RATING_CLEAN = "clean"
RATING_UNMARKED = ""


def edition_rating(*texts: str | None) -> str:
    """``"explicit"``, ``"clean"``, or ``""`` for the given title/comment text.

    Whole words only: "uncleaned tape source" is not a censored cut, and a
    release merely mentioning explicitness in prose is not one either. Several
    texts may be passed (a release's disambiguation AND its title) - the first
    positive marker found wins, explicit taking precedence, because a release
    labelled "explicit" that happens to sit in a "clean/explicit" pair should
    never read as the clean one.
    """
    joined = " ".join(text for text in texts if text)
    if _EXPLICIT_MARKERS.search(joined):
        return RATING_EXPLICIT
    if _CLEAN_MARKERS.search(joined):
        return RATING_CLEAN
    return RATING_UNMARKED


def strip_edition_suffix(title: str) -> str:
    stripped = EDITION_SUFFIXES.sub("", title)
    stripped = BRACKETS.sub(" ", stripped)
    return WHITESPACE.sub(" ", stripped).strip()
