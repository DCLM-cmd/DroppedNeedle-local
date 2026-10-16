"""Other spellings an indexer may file a name under.

Shared files and Usenet releases rarely keep MusicBrainz's spelling: German umlauts turn
up transliterated (``Zähne`` -> ``Zaehne``) or simply stripped (``Zahne``), and dotted
acronyms lose their dots (``K.I.Z`` -> ``KIZ``). Indexers match words literally, so a
search under one spelling misses releases filed under another.
"""

from __future__ import annotations

import re
import unicodedata

_GERMAN = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "ẞ": "SS"}
)
# Two or more single letters each followed by a dot, optionally closed by one more
# letter: K.I.Z / K.I.Z. / A.S.A.P. - but not "Dr." or "Mr.", which are words.
_DOTTED_ACRONYM = re.compile(r"(?<![^\W\d_])(?:[^\W\d_]\.){2,}(?:[^\W\d_](?![^\W\d_])\.?)?")


def collapse_acronyms(text: str) -> str:
    """``K.I.Z.`` -> ``KIZ``, ``A.S.A.P. Rocky`` -> ``ASAP Rocky``."""
    return _DOTTED_ACRONYM.sub(lambda match: match.group(0).replace(".", ""), text)


def _plain(text: str) -> str:
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    return unicodedata.normalize("NFC", stripped).replace("ß", "ss").replace("ẞ", "SS")


def spelling_variants(text: str) -> list[str]:
    """``text`` first, then each distinct alternative spelling worth searching for."""
    variants: list[str] = []
    for base in (text, collapse_acronyms(text)):
        for variant in (base, base.translate(_GERMAN), _plain(base)):
            variant = " ".join(variant.split())
            if variant and variant not in variants:
                variants.append(variant)
    return variants
