"""F-059: the evidence engine's comparison fold must agree with the
recall-side matchers - ligatures and heavy diacritics (æ/ø/ß) that clear
text recall must not degrade to review purely because of folding."""

from services.native.album_evidence_engine import _distance, _fold


def test_ligature_folds_to_ascii_pair():
    # ÆNIMA (MB) vs Aenima (local tags) must compare as equal.
    assert _fold("Ænima") == _fold("Aenima")
    assert _distance("Ænima", "Aenima") == 0.0


def test_slash_o_and_eszett_fold():
    assert _fold("Bjørn") == _fold("Bjorn")
    assert _fold("Straße") == _fold("Strasse")
    assert _distance("Bjørn", "Bjorn") == 0.0


def test_accents_still_fold_through_nfkd():
    assert _fold("Beyoncé") == _fold("Beyonce")


def test_cjk_is_never_transliterated():
    # D3: CJK stays verbatim through the fold (only case/punct-stripped).
    folded = _fold("桃源へ")
    assert "\u6843" in folded and "\u6e90" in folded


def test_distance_threshold_no_longer_downgrades_ligature_titles():
    # The album-title class threshold is 0.20; before F-059 this pair sat at
    # distance 1.0 (untransliterable chars stripped to nothing on one side).
    assert _distance("Ænima", "Aenima") <= 0.20
