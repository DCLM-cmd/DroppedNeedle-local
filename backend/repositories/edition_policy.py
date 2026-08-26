"""Shared best-edition ranking policy (EditionsEtc Phase 0).

One tie-break vocabulary for ranking sibling editions inside a release
group, consumed at both evaluation moments of NEW-DECISION-02
(.dev-notes/LibraryAudit/DECISIONS-LIVE.md): recall time (release-group
metadata only - no evidence score exists yet) and evidence time (scored
candidates). Pure functions only: no I/O, no provider calls, no
dependencies beyond the standard library.
"""

import re

# Sentinels keep mixed-precision ordering explicit: regex-valid date
# components are two digits (<= 99) and valid years are four digits
# (<= 9999), so these sentinels can never collide with a parsed value.
_UNKNOWN_COMPONENT = 100
_INVALID_YEAR = 10000

# Signed NEW-DECISION-02 automatic-acceptance thresholds.
MIN_AUTO_ACCEPT_SCORE = 0.95
AUTO_ACCEPT_MARGIN = 0.05

# Evidence reasons that may drive an AUTOMATIC exact-edition acceptance
# (D-EDITION-AUTO). ``ACCEPTED`` deliberately stays manual-only: its
# provenance is not defined tightly enough for unattended catalog writes,
# and this set must never be widened into
# ``AUTOMATIC_SAFE_EVIDENCE_REASONS`` (which keeps ``ACCEPTED`` for the
# explicit/manual paths) without a signed owner decision.
AUTO_ACCEPT_EVIDENCE_REASONS = frozenset(
    {"SUPPORTED", "SUPPORTED_EMBEDDED_IDS"}
)


def edition_date_key(date_str: str | None) -> tuple[int, int, int]:
    """F-EDITION-02 explicit mixed-precision ISO date ordering key.

    Supported MusicBrainz shapes are ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``.
    Known components compare chronologically; when two values share the
    same known prefix, the MORE precise value sorts FIRST, so a fully
    dated release never loses to an ambiguous year-only value of the same
    year. Precision is never invented (D18): unknown month/day use the
    sentinel ``100``, which sorts after every real component (<= 99).
    Missing or unparsable input shares one key pinned at year ``10000``
    that sorts after every valid date (+infinity equivalent); the input
    string is never rewritten.
    """
    match = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$", (date_str or "").strip())
    if not match:
        return (_INVALID_YEAR, _UNKNOWN_COMPONENT, _UNKNOWN_COMPONENT)
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) is not None else _UNKNOWN_COMPONENT
    day = int(match.group(3)) if match.group(3) is not None else _UNKNOWN_COMPONENT
    return (year, month, day)


def recall_key(release: dict, target_track_count: int) -> tuple | None:
    """Recall-time ranking key for sibling editions in one release group.

    NEW-DECISION-02 orders editions by: evidence score -> Official status
    -> parsed date with explicit precision -> XW country preference ->
    release MBID. The evidence-score term is absent here BY CONSTRUCTION:
    recall runs on release-group search metadata before any candidate
    release has been fetched and scored, so this key is the signed order
    minus its leading score term.

    Returns ``None`` for releases with no usable id or zero total medium
    track-count; those carry no medium data to rank against and are
    skipped consistently by every lane (unchanged F-062 semantics).
    """
    release_id = release.get("id")
    if not release_id:
        return None
    track_count = sum(
        int(medium.get("track-count") or 0)
        for medium in release.get("media") or []
    )
    if track_count <= 0:
        return None
    return (
        abs(track_count - target_track_count),
        0 if release.get("status") == "Official" else 1,
        edition_date_key(release.get("date")),
        0 if release.get("country") == "XW" else 1,
        release_id,
    )


def auto_accept_decision(ranked: list[tuple[tuple, float]]) -> tuple[bool, str]:
    """Signed NEW-DECISION-02 automatic-acceptance gate (D-EDITION-AUTO).

    ``ranked`` holds ``(sort_key, evidence_score)`` pairs sorted best
    first by the caller. Returns ``(True, "AUTO_ACCEPT")`` only when the
    list is non-empty, the top score is >= 0.95, and there is either
    exactly one candidate or the top beats the second by >= 0.05 while
    its full sort key differs from EVERY other candidate's key.

    Reason codes, checked in order:
    - ``"EMPTY"``: nothing to accept.
    - ``"BELOW_MIN_SCORE"``: top score under 0.95.
    - ``"TIE"``: another candidate shares the top sort key. Equal keys
      are indistinguishable under the approved order - including
      partial-date ties (D18) - so they go to review regardless of any
      score gap.
    - ``"MARGIN_TOO_NARROW"``: distinct keys but the top-vs-second score
      gap is under 0.05.
    """
    if not ranked:
        return False, "EMPTY"
    top_key, top_score = ranked[0]
    if top_score < MIN_AUTO_ACCEPT_SCORE:
        return False, "BELOW_MIN_SCORE"
    if len(ranked) == 1:
        return True, "AUTO_ACCEPT"
    if any(top_key == other_key for other_key, _ in ranked[1:]):
        return False, "TIE"
    if top_score - ranked[1][1] < AUTO_ACCEPT_MARGIN:
        return False, "MARGIN_TOO_NARROW"
    return True, "AUTO_ACCEPT"
