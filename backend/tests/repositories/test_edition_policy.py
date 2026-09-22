"""Unit tests for the shared best-edition ranking policy (Phase 0)."""

from repositories.edition_policy import (
    auto_accept_decision,
    edition_date_key,
    evidence_key,
    recall_key,
)

# Phase-0 shared RG fixture (LibraryFindings-All X-04 step 0.3, E-01/E-04
# pre-work): siblings varying status/date-precision/country/MBID/track-count,
# mirrored in tests/services/test_edition_selection.py (display divergence)
# and tests/services/native/test_album_evidence_engine.py (decide order).
# The MBIDs are chosen so bare MBID order (XW first) disagrees with recall
# order (US first) - the 10-track-US vs 12-track-XW divergence proof. Both
# leaders are Official so the display lane (which drops non-Official) still
# ranks them; the Bootleg sibling pins the status axis for recall.
SHARED_RG_MBDID = "rg-shared-edition-divergence"
SHARED_REL_US_10 = {
    "id": "rel-ffff-us-10",
    "status": "Official",
    "date": "2024-01-31",
    "country": "US",
    "media": [{"track-count": 10}],
}
SHARED_REL_XW_12 = {
    "id": "rel-0000-xw-12",
    "status": "Official",
    "date": "2024",
    "country": "XW",
    "media": [{"track-count": 12}],
}
SHARED_REL_BOOTLEG_10 = {
    "id": "rel-eeee-bootleg-10",
    "status": "Bootleg",
    "date": "2024-01-31",
    "country": "US",
    "media": [{"track-count": 10}],
}
SHARED_TARGET_TRACKS = 10

KEY_A = ("release-a",)
KEY_B = ("release-b",)


def test_edition_date_key_missing_and_garbage_share_latest_key():
    invalid = edition_date_key(None)
    assert invalid == edition_date_key("")
    assert invalid == edition_date_key("   ")
    assert invalid == edition_date_key("not-a-date")
    # every valid date sorts before the missing/unparsable key
    assert edition_date_key("1999") < invalid
    assert edition_date_key("2024-02") < invalid


def test_edition_date_key_precision_beats_vagueness_within_prefix():
    assert edition_date_key("2024-06-15") < edition_date_key("2024-06")
    assert edition_date_key("2024-06-15") < edition_date_key("2024")
    assert edition_date_key("2024-06") < edition_date_key("2024")


def test_edition_date_key_chronological_for_known_components():
    assert edition_date_key("2023-12-31") < edition_date_key("2024-01-01")
    assert edition_date_key("2024-01") < edition_date_key("2024-02")
    assert edition_date_key("2024-05-31") < edition_date_key("2024-06-01")


def test_edition_date_key_returns_three_ints_and_strips_whitespace():
    key = edition_date_key(" 2024-06-15 ")
    assert len(key) == 3
    assert all(isinstance(part, int) for part in key)


def test_evidence_key_orders_score_official_date_xw_mbid():
    us = evidence_key(0.9, "Official", "2024-01-31", "US", "rel-ffff-us-10")
    xw = evidence_key(0.9, "Official", "2024", "XW", "rel-0000-xw-12")
    bootleg = evidence_key(0.9, "Bootleg", "2024-01-31", "US", "rel-eeee-bootleg-10")
    # score leads: a higher score wins regardless of the tail
    assert evidence_key(0.91, "Bootleg", None, "US", "rel-a") < evidence_key(
        0.9, "Official", "2024-01-31", "XW", "rel-a"
    )
    # signed tail at tied scores: Official first (both of them), then the
    # precise US date over the vague XW year; the Bootleg trails on status
    # even with a precise date and US country
    assert us < xw < bootleg
    assert us[1] == 0 and bootleg[1] == 1
    assert us[2] == edition_date_key("2024-01-31")
    assert xw[2] == edition_date_key("2024")
    assert us[3] == 1 and xw[3] == 0
    # bare MBID order disagrees with the signed tail (XW MBID is smaller)
    assert xw[4] < us[4]


def test_evidence_key_tolerates_missing_terms():
    assert evidence_key(0.9, None, None, None, None) == (
        -0.9,
        1,
        edition_date_key(None),
        1,
        "",
    )
    # missing terms tie, so a dated edition beats an undated one
    assert evidence_key(0.9, None, "2024-01-31", None, "rel-b") < evidence_key(
        0.9, None, None, None, "rel-a"
    )


def test_recall_key_returns_none_without_usable_id_or_track_count():
    assert recall_key({}, 10) is None
    assert recall_key({"id": "rel"}, 10) is None
    assert recall_key({"id": "rel", "media": [{"track-count": 0}]}, 10) is None


def test_recall_key_orders_proximity_status_date_country_mbid():
    target = 10
    near = recall_key(
        {
            "id": "near",
            "status": "Official",
            "country": "XW",
            "date": "2024-01-31",
            "media": [{"track-count": 10}],
        },
        target,
    )
    far = recall_key(
        {
            "id": "far",
            "status": "Bootleg",
            "country": "US",
            "date": None,
            "media": [{"track-count": 12}],
        },
        target,
    )
    assert near is not None and far is not None
    assert near < far
    # signed order minus score: proximity, Official, parsed date, XW, MBID
    assert near[0] == 0 and far[0] == 2
    assert near[1] == 0 and far[1] == 1
    assert near[2] == edition_date_key("2024-01-31")
    assert far[2] == edition_date_key(None)
    assert near[3] == 0 and far[3] == 1
    assert (near[4], far[4]) == ("near", "far")


def test_recall_key_year_only_loses_to_dated_sibling_of_same_year():
    year_only = recall_key(
        {"id": "vague", "status": "Official", "date": "2024",
         "media": [{"track-count": 9}]},
        10,
    )
    dated = recall_key(
        {"id": "precise", "status": "Official", "date": "2024-01-31",
         "media": [{"track-count": 11}]},
        10,
    )
    assert year_only is not None and dated is not None
    assert dated < year_only


def test_auto_accept_empty_list_reviews():
    assert auto_accept_decision([]) == (False, "EMPTY")


def test_auto_accept_single_candidate_score_boundary():
    # exactly the 0.95 minimum accepts; anything under reviews
    assert auto_accept_decision([(KEY_A, 0.95)]) == (True, "AUTO_ACCEPT")
    assert auto_accept_decision([(KEY_A, 0.9499)]) == (False, "BELOW_MIN_SCORE")


def test_auto_accept_margin_boundary():
    # float-representative pair for an exactly-0.05 winner margin: accepted
    assert auto_accept_decision([(KEY_A, 1.0), (KEY_B, 0.95)]) == (
        True,
        "AUTO_ACCEPT",
    )
    # narrower margin goes to review even above the min score
    assert auto_accept_decision([(KEY_A, 0.96), (KEY_B, 0.92)]) == (
        False,
        "MARGIN_TOO_NARROW",
    )


def test_auto_accept_equal_keys_tie_to_review_regardless_of_gap():
    # decisive score gap cannot rescue an indistinguishable policy tie
    assert auto_accept_decision([(KEY_A, 0.99), (KEY_A, 0.90)]) == (False, "TIE")
    # equal keys AND equal scores are still a TIE, never MARGIN_TOO_NARROW
    assert auto_accept_decision([(KEY_A, 0.99), (KEY_A, 0.99)]) == (False, "TIE")


def test_auto_accept_tie_against_non_adjacent_candidate():
    # top key repeats at rank 3: differs-from-every-other-key fails
    ranked = [(KEY_A, 0.99), (KEY_B, 0.90), (KEY_A, 0.80)]
    assert auto_accept_decision(ranked) == (False, "TIE")


def test_auto_accept_min_score_precedes_tie_and_margin_codes():
    assert auto_accept_decision([(KEY_A, 0.90), (KEY_A, 0.10)]) == (
        False,
        "BELOW_MIN_SCORE",
    )


def test_auto_accept_two_distinct_candidates_accept():
    assert auto_accept_decision([(KEY_A, 0.98), (KEY_B, 0.90)]) == (
        True,
        "AUTO_ACCEPT",
    )


def test_shared_rg_recall_orders_proximity_status_date_country_mbid():
    """0.3 shared fixture: proximity first, then the signed tail - the
    10-track US edition beats the 12-track XW edition despite the larger
    MBID and the XW country preference."""
    us = recall_key(SHARED_REL_US_10, SHARED_TARGET_TRACKS)
    xw = recall_key(SHARED_REL_XW_12, SHARED_TARGET_TRACKS)
    bootleg = recall_key(SHARED_REL_BOOTLEG_10, SHARED_TARGET_TRACKS)
    assert us is not None and xw is not None and bootleg is not None
    assert sorted(
        [SHARED_REL_US_10, SHARED_REL_XW_12, SHARED_REL_BOOTLEG_10],
        key=lambda release: recall_key(release, SHARED_TARGET_TRACKS),
    ) == [SHARED_REL_US_10, SHARED_REL_BOOTLEG_10, SHARED_REL_XW_12]
    # Proximity decides US-10 over XW-12 before any tail term is reached.
    assert us[0] == 0 and xw[0] == 2
    # Status decides US-10 over the Bootleg-10 at tied proximity.
    assert us[1] == 0 and bootleg[1] == 1
    assert us[2] == edition_date_key("2024-01-31")
    assert xw[2] == edition_date_key("2024")
    # Bare MBID order disagrees (the divergence proof): recall never ranks
    # by MBID first.
    assert xw[4] < us[4]


def test_shared_rg_partial_date_precision_routes_to_review_not_order():
    """0.3 D18 edge: a year-only vs full-date pair (`2024` vs `2024-01-31`,
    all else equal) pins review-direction, not rank order. With tied
    evidence scores the margin is zero, so the signed gate refuses to
    auto-accept; precision itself is never invented (unequal date keys)."""
    vague = dict(SHARED_REL_US_10, id="rel-1111-vague-10", date="2024")
    precise = dict(SHARED_REL_US_10, id="rel-2222-precise-10", date="2024-01-31")
    vague_key = recall_key(vague, SHARED_TARGET_TRACKS)
    precise_key = recall_key(precise, SHARED_TARGET_TRACKS)
    assert vague_key is not None and precise_key is not None
    assert vague_key != precise_key
    assert auto_accept_decision([(precise_key, 0.97), (vague_key, 0.97)]) == (
        False,
        "MARGIN_TOO_NARROW",
    )
    # Deliberately no rank-order assert between the pair: D18 pins the
    # review direction (tie-per-precision), never the order.
