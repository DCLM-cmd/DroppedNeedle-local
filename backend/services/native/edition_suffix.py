"""Shared edition-suffix normalization for album title comparison.

One maintained implementation of the provider-matching suffix convention
(F-MATCH-01): ``musicbrainz_matcher`` and the target evidence engine must fold
album titles through this helper so both surfaces compare the same base title.
The suffix set is an existing provider-matching convention; extend it only with
a separately evidenced provider qualifier.
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


def strip_edition_suffix(title: str) -> str:
    """Remove edition qualifiers + leftover brackets, collapse whitespace."""
    stripped = EDITION_SUFFIXES.sub("", title)
    stripped = BRACKETS.sub(" ", stripped)
    return WHITESPACE.sub(" ", stripped).strip()
