"""Unit tests for the shared best-edition ranking policy (Phase 0)."""

from repositories.edition_policy import (
    auto_accept_decision,
    edition_date_key,
    recall_key,
)

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
