"""Unit tests for the audio quality tier helpers."""

from repositories.protocols.download_client import DownloadSearchResult
import pytest

from services.native.quality_tiers import (
    candidate_tier,
    file_tier,
    folder_hires_key,
    in_range,
    is_flac_or_mp3,
    should_acquire,
    tier_for,
    tier_rank,
)


def _f(
    filename: str,
    *,
    ext: str = "",
    bitrate: int | None = None,
    bit_depth: int | None = None,
    sample_rate: int | None = None,
) -> DownloadSearchResult:
    return DownloadSearchResult(
        username="u",
        filename=filename,
        parent_directory="p",
        size=1,
        extension=ext,
        bitrate=bitrate,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
    )


def test_file_tier_lossless_by_extension():
    assert file_tier(_f("a.flac")) == "lossless"
    assert file_tier(_f("a", ext="flac")) == "lossless"  # extension field wins
    assert file_tier(_f("a.alac")) == "lossless"


def test_file_tier_lossy_bitrate_bands():
    assert file_tier(_f("a.mp3", bitrate=320)) == "mp3_320"
    assert file_tier(_f("a.mp3", bitrate=256)) == "mp3_256"
    assert file_tier(_f("a.mp3", bitrate=192)) == "mp3_192"
    assert file_tier(_f("a.mp3", bitrate=128)) == "low"
    assert file_tier(_f("a.mp3", bitrate=None)) == "low"


def test_file_tier_codec_agnostic_for_lossy():
    # m4a / opus / ogg map onto the same bitrate bands as mp3
    assert file_tier(_f("a.m4a", bitrate=320)) == "mp3_320"
    assert file_tier(_f("a.opus", bitrate=256)) == "mp3_256"
    assert file_tier(_f("a.ogg", bitrate=192)) == "mp3_192"


def test_candidate_tier_is_worst_file():
    assert candidate_tier([_f("a.flac"), _f("b.mp3", bitrate=320)]) == "mp3_320"
    assert candidate_tier([]) == "low"


def test_hires_folder_uses_conservative_defaults_for_missing_file_metadata():
    known = _f("a.flac", bit_depth=24, sample_rate=96_000)
    unknown = _f("b.flac")

    assert folder_hires_key([known, unknown]) == (16, 44_100)


def test_in_range_inclusive():
    assert in_range("mp3_320", "mp3_320", "lossless")
    assert in_range("lossless", "mp3_320", "lossless")
    assert not in_range("mp3_192", "mp3_320", "lossless")
    assert not in_range("mp3_320", "lossless", "lossless")  # only-lossless excludes mp3


def test_is_flac_or_mp3():
    assert is_flac_or_mp3(_f("a.flac"))
    assert is_flac_or_mp3(_f("a.mp3"))
    assert not is_flac_or_mp3(_f("a.ogg"))
    assert not is_flac_or_mp3(_f("a.m4a"))
    assert not is_flac_or_mp3(_f("a.alac"))  # lossless, but not FLAC/MP3


def test_tier_rank_order():
    assert (
        tier_rank("lossless")
        > tier_rank("mp3_320")
        > tier_rank("mp3_192")
        > tier_rank("low")
    )


def test_tier_for_matches_file_tier():
    # tier_for(format, bitrate) is the shared classifier behind file_tier + the library lookup.
    assert tier_for("flac", None) == "lossless"
    assert (
        tier_for(".FLAC", None) == "lossless"
    )  # case/dot tolerant (library file_format)
    assert tier_for("mp3", 320) == "mp3_320"
    assert tier_for("mp3", 256) == "mp3_256"
    assert tier_for("mp3", 200) == "mp3_192"
    assert tier_for("mp3", 96) == "low"
    assert tier_for("", None) == "low"
    assert tier_for("alac", 0) == "lossless"


def test_should_acquire_not_in_library():
    assert should_acquire(None, "lossless", upgrade_allowed=False) is True
    assert should_acquire(None, "mp3_320", upgrade_allowed=True) is True


def test_should_acquire_upgrades_off_skips_any_held_copy():
    # Default behaviour (binary has_album): hold it at all -> skip.
    assert should_acquire("low", "lossless", upgrade_allowed=False) is False
    assert should_acquire("lossless", "lossless", upgrade_allowed=False) is False


def test_should_acquire_upgrades_on_only_below_cutoff():
    # Below the cutoff -> re-acquire (upgrade); at/above -> satisfied.
    assert should_acquire("mp3_320", "lossless", upgrade_allowed=True) is True
    assert should_acquire("low", "mp3_320", upgrade_allowed=True) is True
    assert should_acquire("lossless", "lossless", upgrade_allowed=True) is False
    assert should_acquire("mp3_320", "mp3_320", upgrade_allowed=True) is False


# F-EDITION-04: MP4-family lossless requires bit-depth evidence

def test_tier_for_alac_m4a_with_depth_is_lossless():
    from repositories.protocols.download_client import DownloadSearchResult
    file = DownloadSearchResult(
        username="u", filename="a.m4a", parent_directory="p",
        size=30_000_000, extension="m4a", bitrate=900, bit_depth=16,
    )
    assert file_tier(file) == "lossless"
    assert tier_for("m4a", 900, 16) == "lossless"


def test_tier_for_aac_m4a_without_depth_stays_bitrate_tiered():
    from repositories.protocols.download_client import DownloadSearchResult
    file = DownloadSearchResult(
        username="u", filename="a.m4a", parent_directory="p",
        size=10_000_000, extension="m4a", bitrate=256, bit_depth=None,
    )
    assert file_tier(file) == "mp3_256"
    assert tier_for("m4a", 256, None) == "mp3_256"


@pytest.mark.parametrize("ext", ["m4a", "mp4", "mov"])
def test_tier_for_mp4_family_with_depth_is_lossless(ext):
    assert tier_for(ext, 900, 24) == "lossless"
    assert tier_for(ext, None, 16) == "lossless"


def test_tier_for_flac_mp3_only_gate_unchanged_for_alac_m4a():
    # flac_mp3_only is enforced elsewhere (is_flac_or_mp3); an ALAC M4A may be
    # classified lossless here but stays outside the FLAC/MP3-only set.
    from repositories.protocols.download_client import DownloadSearchResult
    file = DownloadSearchResult(
        username="u", filename="a.m4a", parent_directory="p",
        size=30_000_000, extension="m4a", bitrate=900, bit_depth=16,
    )
    assert is_flac_or_mp3(file) is False
