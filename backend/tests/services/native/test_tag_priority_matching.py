"""Tags decide which track a downloaded file is; the clock and AcoustID only corroborate.

The reported symptom, in the user's words: all the files were there and their tags were
right, but they came out wrongly sorted - and DroppedNeedle declared the tags wrong
rather than simply using them.

Two places caused that. Matching gated on duration BEFORE reading any tag, so a
remaster or an edit whose length differs from MusicBrainz's was thrown out while its
tags named the track exactly. And a confident AcoustID result could overrule a
MusicBrainz recording id written into the file itself, holding it as a
``fingerprint_mismatch``.
"""

from types import SimpleNamespace

import pytest

from models.download_manifest import ExpectedTrack
from services.native.file_processor import (
    _TAG_IDENTITY_CERTAIN,
    _TAG_IDENTITY_NONE,
    _TAG_IDENTITY_POSITIONAL,
    _fingerprint_disagrees,
    _pair_score,
    tag_identity,
)

_RECORDING = "11111111-2222-3333-4444-555555555555"
_RELEASE_TRACK = "66666666-7777-8888-9999-aaaaaaaaaaaa"


def _tag(**over):
    base = dict(
        title="Burn the Witch",
        artist="Radiohead",
        album="A Moon Shaped Pool",
        track_number=1,
        disc_number=1,
        musicbrainz_recording_id=None,
        musicbrainz_release_track_id=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _candidate(*, duration=None, **tag_over):
    return SimpleNamespace(
        path=__import__("pathlib").Path("01 - Burn the Witch.flac"),
        tag=_tag(**tag_over),
        info=SimpleNamespace(duration_seconds=duration, file_format="flac", bitrate=None),
    )


def _track(**over):
    base = dict(
        track_number=1,
        disc_number=1,
        duration_seconds=221.0,
        recording_mbid=_RECORDING,
        title="Burn the Witch",
        release_track_mbid=_RELEASE_TRACK,
    )
    base.update(over)
    return ExpectedTrack(**base)


# ---- what counts as the tags naming a track ----------------------------------------

def test_a_recording_id_is_certain_identity() -> None:
    tag = _tag(musicbrainz_recording_id=_RECORDING)

    assert tag_identity(tag, 1, _track()) == _TAG_IDENTITY_CERTAIN


def test_a_release_track_id_is_certain_identity() -> None:
    tag = _tag(musicbrainz_release_track_id=_RELEASE_TRACK)

    assert tag_identity(tag, 1, _track()) == _TAG_IDENTITY_CERTAIN


def test_an_exact_title_at_the_exact_position_is_strong_not_certain() -> None:
    """Strong enough to widen the tolerance, not strong enough to remove it: two
    albums can hold a same-named song at the same number."""
    assert tag_identity(_tag(), 1, _track()) == _TAG_IDENTITY_POSITIONAL


def test_the_right_title_at_the_wrong_position_is_not_identity() -> None:
    assert tag_identity(_tag(track_number=7), 1, _track()) == _TAG_IDENTITY_NONE


def test_a_different_title_is_not_identity() -> None:
    assert tag_identity(_tag(title="Daydreaming"), 1, _track()) == _TAG_IDENTITY_NONE


def test_an_untagged_file_has_no_identity() -> None:
    assert tag_identity(None, 1, _track()) == _TAG_IDENTITY_NONE


# ---- the duration gate no longer overrules the tags --------------------------------

def test_a_remaster_matches_when_its_recording_id_says_so() -> None:
    """A minute longer than MusicBrainz records, and unmistakably the same track.
    The old gate (15s or 10%) threw this out before reading a single tag."""
    candidate = _candidate(duration=283.0, musicbrainz_recording_id=_RECORDING)

    assert _pair_score(candidate, _track()) is not None


def test_an_edit_matches_on_an_exact_title_at_the_right_position() -> None:
    candidate = _candidate(duration=180.0)  # 41s short: outside the ordinary gate

    assert _pair_score(candidate, _track()) is not None


def test_an_absurd_length_is_still_refused_for_a_title_match() -> None:
    """Widened, not removed: a whole-album file at position 1 is not track 1."""
    candidate = _candidate(duration=3200.0)

    assert _pair_score(candidate, _track()) is None


def test_a_certain_tag_accepts_any_length() -> None:
    """An id IS the identity. A single-file album image tagged with one recording id
    is a real shape, and refusing it on length would refuse the truth."""
    candidate = _candidate(duration=3200.0, musicbrainz_recording_id=_RECORDING)

    assert _pair_score(candidate, _track()) is not None


def test_an_untagged_file_is_still_gated_on_duration() -> None:
    """Nothing here loosens matching for files that say nothing about themselves."""
    candidate = _candidate(duration=400.0, title="", track_number=0)

    assert _pair_score(candidate, _track()) is None


def test_a_close_length_still_scores_higher_than_a_distant_one() -> None:
    """Duration stops being a gate; it does not stop being evidence."""
    close = _candidate(duration=222.0, musicbrainz_recording_id=_RECORDING)
    far = _candidate(duration=280.0, musicbrainz_recording_id=_RECORDING)

    assert _pair_score(close, _track()) > _pair_score(far, _track())


# ---- AcoustID does not overrule a MusicBrainz id in the file -----------------------

def _confident_fingerprint(title="Something Else Entirely", artist="Another Band"):
    return SimpleNamespace(status="pass", title=title, artist=artist, score=0.95)


def test_acoustid_cannot_overrule_a_certain_tag() -> None:
    """83 files were held as fingerprint_mismatch. A tag written into the file by
    whoever prepared the release outranks an inference over a crowd-sourced database."""
    assert not _fingerprint_disagrees(
        _confident_fingerprint(),
        _track(),
        "Radiohead",
        tag_identity_level=_TAG_IDENTITY_CERTAIN,
    )


def test_acoustid_still_catches_a_wrong_song_when_the_tags_are_silent() -> None:
    """The guard has to keep working: this is how a mislabelled rip is caught."""
    assert _fingerprint_disagrees(
        _confident_fingerprint(),
        _track(),
        "Radiohead",
        tag_identity_level=_TAG_IDENTITY_NONE,
    )


def test_a_merely_positional_tag_does_not_silence_acoustid() -> None:
    """Only an id earns that. A title at a position can coincide."""
    assert _fingerprint_disagrees(
        _confident_fingerprint(),
        _track(),
        "Radiohead",
        tag_identity_level=_TAG_IDENTITY_POSITIONAL,
    )


def test_an_unconfident_fingerprint_never_rejects() -> None:
    assert not _fingerprint_disagrees(
        SimpleNamespace(status="skip", title=None, artist=None),
        _track(),
        "Radiohead",
    )
