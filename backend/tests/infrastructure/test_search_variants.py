"""Spellings an indexer may file a name under."""

from infrastructure.search_variants import collapse_acronyms, spelling_variants


def test_umlauts_are_offered_transliterated_and_plain():
    assert spelling_variants("Kollegah 32 Zähne") == [
        "Kollegah 32 Zähne",
        "Kollegah 32 Zaehne",
        "Kollegah 32 Zahne",
    ]
    assert "Strasse" in " ".join(spelling_variants("Straße"))


def test_dotted_acronyms_are_offered_without_dots():
    assert spelling_variants("K.I.Z Hurra die Welt geht unter") == [
        "K.I.Z Hurra die Welt geht unter",
        "KIZ Hurra die Welt geht unter",
    ]
    assert collapse_acronyms("K.I.Z.") == "KIZ"
    assert collapse_acronyms("A.S.A.P. Rocky") == "ASAP Rocky"


def test_ordinary_names_and_abbreviations_are_left_alone():
    assert spelling_variants("Radiohead In Rainbows") == ["Radiohead In Rainbows"]
    assert collapse_acronyms("Dr. Dre") == "Dr. Dre"
    assert collapse_acronyms("Jay-Z") == "Jay-Z"
    assert spelling_variants("坂本龍一") == ["坂本龍一"]
