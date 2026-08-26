"""Wrong-edition spec (shared, both sources - parsing finding M3).

Reject a candidate whose name carries a different-product marker (live /
bootleg / boxset / discography / compilation / greatest-hits / karaoke /
tribute / instrumental ...) that the requested album+artist lacks - almost
certainly a different product than the studio album asked for. Gated on the
request, so a genuine live/boxset request keeps its live/boxset releases.

Lifted from the Usenet-only ``_is_wrong_edition`` (``newznab_release_scorer``)
so the Soulseek folder path gets the identical hard reject (M3): Soulseek used
to have no edition reject and would grab a live album for a studio request.
"remaster"/"deluxe" are deliberately ABSENT - legit versions of the album.
Lidarr ref: release-to-album match + Discography + Ignored-terms.
"""

import re

from models.download import TargetAlbum

from ..context import DecisionContext
from ..decision import Accept, Candidate, Decision, Disposition, Reject, RejectCode, SpecPolicy

_WRONG_EDITION_RE = re.compile(
    r"\b(live|bootleg|box[\s\-]?set|discograph(?:y|ie)|anthology|compilation|collection|"
    r"definitive|greatest[\s\-]hits|best[\s\-]of|karaoke|tribute|instrumental|"
    r"a[\s\-]?cappella|acapella|outtakes?|rehearsals?|"
    # PLURAL only on the "complete ... albums/recordings/works" discography
    # pattern: singular "Complete Album" is an uploader's "full album, not a
    # teaser" tag, not a multi-album product (else it hard-rejects the
    # requested album on the obfuscated-artist path, where wrong_album defers).
    r"complete[\s\-](?:studio[\s\-])?(?:recordings?|works|albums|collection|box))\b",
    re.IGNORECASE,
)


def wrong_edition(
    candidate: Candidate,
    target: TargetAlbum,
    context: DecisionContext,
    policy: SpecPolicy,
) -> Decision:
    # F-EDITION-03: flatten ALL separator variants to spaces before matching -
    # ``_`` is a regex word char (so ``\blive\b`` would MISS
    # ``Led_Zeppelin-Live_EP``), and ``Box.Set`` must read as ``box set`` so
    # the dotted spelling cannot slip past the box-set product marker.
    title = (candidate.match_text or "").replace("_", " ").replace(".", " ")
    wanted = f"{target.album_title} {target.artist_name}".lower()
    for match in _WRONG_EDITION_RE.finditer(title):
        term = " ".join(match.group(0).lower().replace("-", " ").split())
        if term not in wanted:
            return Reject(
                code=RejectCode.WRONG_EDITION,
                detail=f"edition marker {term!r} not in the requested album",
                disposition=Disposition.PERMANENT,
            )
    return Accept()
