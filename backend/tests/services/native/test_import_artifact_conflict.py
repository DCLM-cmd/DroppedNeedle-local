"""A differing album cover must not block the audio import.

Artwork is per-ALBUM and decorative. When an upgrade's release shipped a different
cover than the one already on disk, the publisher raised a destination conflict and
the WHOLE bundle was held - so an mp3 -> flac upgrade could never publish, and the
user saw only "a planned destination is occupied by different content" with no way
to tell that the obstacle was a picture.

The existing file wins: identical bytes are not a conflict at all, different bytes
mean the album already has artwork and the incoming one is dropped from the plan.
"""

import hashlib
from types import SimpleNamespace

import pytest

from services.native.library_management_publisher import LibraryManagementPublisher


def _artifact(fingerprint):
    return SimpleNamespace(
        kind="cover",
        source_fingerprint=fingerprint,
        destination_root_id="root",
        destination_relative_path="Artist/Album/cover.jpg",
        source_path=None,
    )


def _write(path, data: bytes):
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_nothing_at_the_destination_is_not_taken(tmp_path) -> None:
    destination = tmp_path / "cover.jpg"

    assert not LibraryManagementPublisher._artifact_already_taken(
        destination, _artifact("whatever")
    )


def test_identical_artwork_is_not_taken(tmp_path) -> None:
    """The album's first imported track wrote it; the second must not trip over it."""
    destination = tmp_path / "cover.jpg"
    digest = _write(destination, b"the same picture")

    assert not LibraryManagementPublisher._artifact_already_taken(
        destination, _artifact(digest)
    )


def test_different_artwork_is_taken_and_kept(tmp_path) -> None:
    """The regression: this used to fail the whole bundle instead of the picture."""
    destination = tmp_path / "cover.jpg"
    _write(destination, b"the cover already on disk")

    assert LibraryManagementPublisher._artifact_already_taken(
        destination, _artifact(hashlib.sha256(b"a different cover").hexdigest())
    )


def test_an_unfingerprinted_artifact_never_overwrites(tmp_path) -> None:
    """With nothing to compare against, the file on disk wins."""
    destination = tmp_path / "cover.jpg"
    _write(destination, b"existing")

    assert LibraryManagementPublisher._artifact_already_taken(destination, _artifact(""))
    assert LibraryManagementPublisher._artifact_already_taken(
        destination, _artifact(None)
    )


def test_a_directory_at_the_destination_is_treated_as_taken(tmp_path) -> None:
    """_hash_file refuses a non-regular file; that must read as "leave it alone",
    never as "free to overwrite"."""
    destination = tmp_path / "cover.jpg"
    destination.mkdir()

    assert LibraryManagementPublisher._artifact_already_taken(
        destination, _artifact("abc")
    )


def test_a_symlink_is_treated_as_taken(tmp_path) -> None:
    real = tmp_path / "real.jpg"
    real.write_bytes(b"data")
    link = tmp_path / "cover.jpg"
    link.symlink_to(real)

    assert LibraryManagementPublisher._artifact_already_taken(
        link, _artifact(hashlib.sha256(b"data").hexdigest())
    )
