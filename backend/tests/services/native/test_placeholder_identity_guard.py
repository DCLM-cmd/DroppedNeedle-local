"""A filler artist name is not an identity, and a folder five times too big is not
the album.

Both defects produced the same live symptom: downloads that fetched something
unrelated. The background upgrade sweep substituted the literal string "Unknown"
for an untagged album, ``artist_evidence`` then matched it against any share path
containing the word "Unknown", and the count check scored a 77-file dump as a
flawless 15-track match. A Gregorian chant collection was auto-accepted and imported
as Radiohead's "A Moon Shaped Pool"; the import landed untagged, so the next sweep
found one more identity-less album and the library poisoned itself further each pass.
"""

import pytest

from services.native.album_preflight_scorer import _count_ratio
from services.native.title_match import (
    artist_evidence,
    is_placeholder_artist,
    names_different_album,
)


@pytest.mark.parametrize(
    "name",
    ["Unknown", "unknown", "  Unknown  ", "Unknown Artist", "Various", "VA",
     "Various Artists", "Untitled", "None", "N/A", "null"],
)
def test_filler_names_are_recognised(name) -> None:
    assert is_placeholder_artist(name)


@pytest.mark.parametrize(
    "name",
    ["Radiohead", "Quadeca", "The Unknown Mortal Orchestra", "Unknown Mortal Orchestra",
     "Various Production", "Nas", "clipping."],
)
def test_real_artists_are_not_filler(name) -> None:
    """The check is the WHOLE name - a real artist whose name merely contains one of
    these words must keep working."""
    assert not is_placeholder_artist(name)


@pytest.mark.parametrize(
    "path",
    [
        "Unknown - (1980) - Salve Regina - Gregorianische Gesange\\01.flac",
        "2003 (Masada Anniversary Edition 3) The Unknown Masada-60'\\01.flac",
        "Unknown\\track.mp3",
        "Unknown 01\\track.mp3",
    ],
)
def test_a_placeholder_artist_is_never_evidence(path) -> None:
    """The live regression, stated directly: these are the exact folders that were
    auto-accepted off the word "Unknown"."""
    assert not artist_evidence("Unknown", path)


def test_a_real_artist_is_still_evidence() -> None:
    assert artist_evidence("Radiohead", "Radiohead\\A Moon Shaped Pool\\01.flac")


def test_a_real_artist_is_still_absent_when_absent() -> None:
    assert not artist_evidence("Radiohead", "Unknown\\Salve Regina\\01.flac")


def test_a_placeholder_does_not_read_as_artist_presence() -> None:
    """``names_different_album`` gates on the artist being present; a placeholder
    matching filler text must not open that gate either."""
    assert not names_different_album(
        "A Moon Shaped Pool", "Unknown", "Unknown - Salve Regina - Gregorian Chant"
    )


def test_an_exact_count_still_scores_full() -> None:
    assert _count_ratio(15, 15) == 1.0


@pytest.mark.parametrize("counted", [16, 18, 20])
def test_bonus_tracks_and_deluxe_editions_still_score_full(counted) -> None:
    """A little over the tracklist is the same album, not a different one."""
    assert _count_ratio(counted, 15) == 1.0


def test_a_short_folder_is_penalised_proportionally() -> None:
    assert _count_ratio(13, 15) == pytest.approx(13 / 15)


def test_a_wildly_oversized_folder_is_penalised() -> None:
    """The regression: 77 files for a 15-track album used to score a perfect 1.0."""
    assert _count_ratio(77, 15) < 0.3


def test_the_penalty_is_a_gradient_not_a_cliff() -> None:
    """Track counts disagree between editions, so the score must decay smoothly."""
    scores = [_count_ratio(n, 15) for n in (20, 25, 30, 45, 77)]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 < s <= 1.0 for s in scores)


def test_an_unknown_track_count_is_neutral() -> None:
    assert _count_ratio(10, 0) == 0.5


# ---- a retry must not carry a placeholder forward -------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from services.native.download_orchestrator import DownloadOrchestrator  # noqa: E402


def _orch(known):
    orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
    orch._store = AsyncMock()
    orch._store.find_release_group_identity = AsyncMock(return_value=known)
    return orch


def _task(artist, album="SCRAPYARD", rg="rg-1"):
    return SimpleNamespace(
        id="t-1", artist_name=artist, album_title=album, release_group_mbid=rg
    )


@pytest.mark.asyncio
async def test_a_placeholder_retry_is_repaired_from_a_sibling_task() -> None:
    """The live regression: a task created as "Unknown" retried as "Unknown" forever,
    burning a search that could never match."""
    orch = _orch(("Quadeca", "SCRAPYARD"))

    assert await orch._retry_identity(_task("Unknown")) == ("Quadeca", "SCRAPYARD")


@pytest.mark.asyncio
async def test_a_real_artist_is_never_looked_up() -> None:
    orch = _orch(("Wrong", "Wrong"))

    assert await orch._retry_identity(_task("Quadeca")) == ("Quadeca", "SCRAPYARD")
    orch._store.find_release_group_identity.assert_not_called()


@pytest.mark.asyncio
async def test_an_unrepairable_placeholder_is_left_as_it_was() -> None:
    """Nothing knows the real name; do not invent a second one."""
    orch = _orch(None)

    assert await orch._retry_identity(_task("Unknown")) == ("Unknown", "SCRAPYARD")


@pytest.mark.asyncio
async def test_without_a_release_group_there_is_nothing_to_look_up() -> None:
    orch = _orch(("Quadeca", "SCRAPYARD"))

    assert await orch._retry_identity(_task("Unknown", rg=None)) == (
        "Unknown", "SCRAPYARD",
    )
    orch._store.find_release_group_identity.assert_not_called()


# ---- a dead release stays blocked across a manual retry --------------------------

@pytest.mark.parametrize(
    "reason", ["verify_failed", "corrupt", "fingerprint_mismatch", "duration_mismatch", "manual"]
)
def test_a_judgement_call_is_reconsidered_on_retry(reason) -> None:
    """These mean "it arrived and we rejected it" - we may have judged wrongly, so an
    explicit try-again should look at it afresh."""
    from infrastructure.persistence.download_store import DownloadStore

    assert reason in DownloadStore._RETRY_CLEARS_QUARANTINE_REASONS


def test_a_release_that_never_downloaded_stays_blocked_on_retry() -> None:
    """The user-visible regression: retry cleared the whole album blocklist, so the
    same dead release - which scores highest - was picked again and failed again
    immediately, with no other source tried."""
    from infrastructure.persistence.download_store import DownloadStore

    assert "download_failed" not in DownloadStore._RETRY_CLEARS_QUARANTINE_REASONS
