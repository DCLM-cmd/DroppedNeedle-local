"""Artwork is served at the size the client asked for.

Finamp requests small images when it draws a grid of covers. DroppedNeedle returned
whatever was cached, at full size, whatever was asked - 484 KB for a 300px tile, a
hundred tiles to a screen. On a phone that reads as covers which load slowly or not at
all. The Jellyfin instance next door answers the same request with 49 KB.
"""

import io

import pytest
from PIL import Image

from infrastructure.images.thumbnails import _cached_resize, resize_to_fit


def _jpeg(size, colour=(120, 60, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _dimensions(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def test_a_large_cover_is_scaled_down_to_the_requested_size() -> None:
    data, content_type = resize_to_fit(_jpeg((1400, 1400)), "image/jpeg", 300)

    assert max(_dimensions(data)) == 300
    assert content_type == "image/jpeg"


def test_scaling_down_actually_saves_transfer() -> None:
    """The point of the exercise, stated as a test rather than assumed."""
    original = _jpeg((1400, 1400))

    smaller, _ = resize_to_fit(original, "image/jpeg", 300)

    assert len(smaller) < len(original) / 4


def test_the_aspect_ratio_is_kept() -> None:
    data, _ = resize_to_fit(_jpeg((1200, 600)), "image/jpeg", 300)

    assert _dimensions(data) == (300, 150)


def test_an_image_already_small_enough_is_passed_through_untouched() -> None:
    original = _jpeg((200, 200))

    data, content_type = resize_to_fit(original, "image/jpeg", 300)

    assert data is original and content_type == "image/jpeg"


def test_no_requested_size_means_the_original() -> None:
    original = _jpeg((1400, 1400))

    assert resize_to_fit(original, "image/jpeg", None)[0] is original


def test_an_absurdly_small_request_is_ignored_rather_than_honoured() -> None:
    """Re-encoding below this costs more than the transfer it saves."""
    original = _jpeg((1400, 1400))

    assert resize_to_fit(original, "image/jpeg", 4)[0] is original


def test_transparency_survives_as_png() -> None:
    """A JPEG cannot carry an alpha channel; flattening one would put a black box
    behind a logo."""
    buffer = io.BytesIO()
    image = Image.new("RGBA", (900, 900), (10, 20, 30, 0))
    image.info["transparency"] = 0
    image.save(buffer, format="PNG")

    data, content_type = resize_to_fit(buffer.getvalue(), "image/png", 200)

    assert content_type == "image/png"
    assert max(_dimensions(data)) == 200


def test_undecodable_bytes_are_returned_unchanged_rather_than_failing() -> None:
    """Artwork is decorative: a client that gets a slightly wrong picture is far
    better off than one that gets an error."""
    junk = b"this is not an image"

    assert resize_to_fit(junk, "image/jpeg", 300) == (junk, "image/jpeg")


def test_empty_bytes_are_returned_unchanged() -> None:
    assert resize_to_fit(b"", "image/jpeg", 300) == (b"", "image/jpeg")


def test_the_same_image_and_size_is_resized_once() -> None:
    """Doing this per request would repeat the one expensive step on every tile."""
    _cached_resize.cache_clear()
    original = _jpeg((1000, 1000))

    first = resize_to_fit(original, "image/jpeg", 300)
    second = resize_to_fit(original, "image/jpeg", 300)

    assert first == second
    info = _cached_resize.cache_info()
    assert (info.hits, info.misses) == (1, 1)


def test_a_different_size_is_produced_separately() -> None:
    _cached_resize.cache_clear()
    original = _jpeg((1000, 1000))

    small, _ = resize_to_fit(original, "image/jpeg", 200)
    large, _ = resize_to_fit(original, "image/jpeg", 600)

    assert max(_dimensions(small)) == 200
    assert max(_dimensions(large)) == 600


@pytest.mark.parametrize("target", [300, 600, 1200])
def test_every_requested_size_comes_back_at_that_size(target) -> None:
    data, _ = resize_to_fit(_jpeg((2000, 2000)), "image/jpeg", target)

    assert max(_dimensions(data)) == target
