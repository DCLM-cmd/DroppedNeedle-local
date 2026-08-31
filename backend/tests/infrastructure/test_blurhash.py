"""BlurHash encoding, checked by decoding the result back.

Finamp warns the user when a Jellyfin server returns no blurhashes ("the server seems
to be misconfigured") and loses its ability to de-duplicate image downloads, so it
re-fetches artwork it already holds.

The encoder is verified by a DECODER written here from the same published spec, not
by comparing against itself: a hash that round-trips to the original colours is
correct, and the decoder is short enough to be obviously right.
"""

import math

import pytest

from infrastructure.images.blurhash import (
    blurhash_for_bytes,
    encode,
    encode_image_bytes,
)

_BASE83 = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"
)


# ---- an independent decoder, per https://blurha.sh -------------------------------

def _decode83(value: str) -> int:
    out = 0
    for char in value:
        out = out * 83 + _BASE83.index(char)
    return out


def _srgb_to_linear(value: int) -> float:
    v = value / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(value: float) -> int:
    v = max(0.0, min(1.0, value))
    if v <= 0.0031308:
        return int(v * 12.92 * 255 + 0.5)
    return int((1.055 * (v ** (1 / 2.4)) - 0.055) * 255 + 0.5)


def _decode_dc(value: int) -> tuple[float, float, float]:
    return (
        _srgb_to_linear(value >> 16),
        _srgb_to_linear((value >> 8) & 255),
        _srgb_to_linear(value & 255),
    )


def _decode_ac(value: int, maximum: float) -> tuple[float, float, float]:
    quant = (value // (19 * 19), (value // 19) % 19, value % 19)
    return tuple(
        math.copysign(abs((q - 9) / 9) ** 2.0, (q - 9) / 9) * maximum for q in quant
    )


def decode(blurhash: str, width: int, height: int) -> list[tuple[int, int, int]]:
    size_flag = _decode83(blurhash[0])
    components_x = (size_flag % 9) + 1
    components_y = (size_flag // 9) + 1
    maximum = (_decode83(blurhash[1]) + 1) / 166

    colours = [_decode_dc(_decode83(blurhash[2:6]))]
    for i in range(1, components_x * components_y):
        colours.append(_decode_ac(_decode83(blurhash[4 + i * 2 : 6 + i * 2]), maximum))

    pixels = []
    for y in range(height):
        for x in range(width):
            r = g = b = 0.0
            for j in range(components_y):
                for i in range(components_x):
                    basis = math.cos(math.pi * x * i / width) * math.cos(
                        math.pi * y * j / height
                    )
                    cr, cg, cb = colours[j * components_x + i]
                    r += cr * basis
                    g += cg * basis
                    b += cb * basis
            pixels.append((_linear_to_srgb(r), _linear_to_srgb(g), _linear_to_srgb(b)))
    return pixels


# ---- the encoder ----------------------------------------------------------------

def _flat(colour, size=16):
    return [colour] * (size * size)


def _average(pixels):
    n = len(pixels)
    return tuple(round(sum(p[c] for p in pixels) / n) for c in range(3))


@pytest.mark.parametrize(
    "colour", [(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 0, 0), (255, 255, 255),
               (128, 64, 32)]
)
def test_a_flat_image_round_trips_to_its_colour(colour) -> None:
    """The strongest single check: decoding must give the colour back.

    A blurhash is lossy by construction, and the sRGB transfer curve is steepest at
    the ends - a fully saturated channel comes back as 250 rather than 255. The
    tolerance covers that without hiding a real error, which would be off by far more
    than a rounding step.
    """
    hashed = encode(_flat(colour), 16, 16)

    decoded = _average(decode(hashed, 8, 8))

    assert all(abs(decoded[c] - colour[c]) <= 8 for c in range(3)), decoded


def test_the_hash_has_the_shape_the_spec_requires() -> None:
    hashed = encode(_flat((10, 120, 200)), 16, 16)

    assert len(hashed) == 6 + 2 * (4 * 4 - 1)  # header + DC + one pair per AC
    assert all(char in _BASE83 for char in hashed)
    assert _decode83(hashed[0]) == (4 - 1) + (4 - 1) * 9  # the 4x4 size flag


def test_a_gradient_keeps_its_light_and_dark_ends() -> None:
    """A blurhash is a low-frequency summary, so structure must survive it."""
    size = 16
    pixels = [
        (int(255 * x / (size - 1)),) * 3 for _ in range(size) for x in range(size)
    ]

    decoded = decode(encode(pixels, size, size), 8, 8)
    left = _average([decoded[row * 8] for row in range(8)])
    right = _average([decoded[row * 8 + 7] for row in range(8)])

    assert left[0] < right[0] - 40


def test_components_are_configurable_and_change_the_length() -> None:
    pixels = _flat((90, 90, 90))

    assert len(encode(pixels, 16, 16, components_x=3, components_y=3)) == 6 + 2 * 8
    assert len(encode(pixels, 16, 16, components_x=5, components_y=5)) == 6 + 2 * 24


@pytest.mark.parametrize(
    "cx,cy", [(0, 4), (4, 0), (10, 4), (4, 10)]
)
def test_out_of_range_components_are_refused(cx, cy) -> None:
    with pytest.raises(ValueError):
        encode(_flat((1, 2, 3)), 16, 16, components_x=cx, components_y=cy)


def test_a_mismatched_pixel_buffer_is_refused() -> None:
    with pytest.raises(ValueError):
        encode([(0, 0, 0)] * 10, 16, 16)


# ---- reading real image bytes ---------------------------------------------------

def _png(colour, size=24):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def test_an_encoded_image_is_hashed_by_its_colour() -> None:
    hashed = encode_image_bytes(_png((200, 30, 40)))

    assert hashed
    assert all(abs(a - b) <= 8 for a, b in zip(_average(decode(hashed, 8, 8)), (200, 30, 40)))


def test_a_jpeg_works_too() -> None:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (24, 24), (20, 180, 90)).save(buffer, format="JPEG")

    assert encode_image_bytes(buffer.getvalue())


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\n truncated"])
def test_unreadable_bytes_yield_no_hash_rather_than_an_error(data) -> None:
    """Artwork is decorative - a cover we cannot decode must never fail a listing."""
    assert encode_image_bytes(data) is None


def test_a_large_image_is_still_cheap() -> None:
    """The encode is bounded by the sample size, not by the source resolution."""
    import time

    data = _png((70, 140, 210), size=1500)
    start = time.perf_counter()
    hashed = encode_image_bytes(data)

    assert hashed
    assert time.perf_counter() - start < 2.0


# ---- memoising by content ---------------------------------------------------------

def test_the_same_content_key_is_encoded_once() -> None:
    """The regression: the local-artwork path had no memo at all, so every listing
    re-encoded every cover - a 100-album page cost 5.3 seconds on EVERY request, not
    just the first."""
    from infrastructure.images.blurhash import _cached_for_key

    _cached_for_key.cache_clear()
    data = _png((11, 22, 33))

    first = blurhash_for_bytes(data, "etag-1")
    second = blurhash_for_bytes(data, "etag-1")

    assert first == second
    info = _cached_for_key.cache_info()
    assert (info.hits, info.misses) == (1, 1)


def test_a_different_content_key_is_encoded_again() -> None:
    """The key is the image's content hash, so new artwork must produce a new hash."""
    from infrastructure.images.blurhash import _cached_for_key

    _cached_for_key.cache_clear()
    red = blurhash_for_bytes(_png((220, 20, 20)), "etag-red")
    blue = blurhash_for_bytes(_png((20, 20, 220)), "etag-blue")

    assert red != blue
    assert _cached_for_key.cache_info().misses == 2


def test_without_a_key_the_memo_is_skipped_not_poisoned() -> None:
    """Caching under a wrong key would serve one album's cover hash for another."""
    from infrastructure.images.blurhash import _cached_for_key

    _cached_for_key.cache_clear()

    assert blurhash_for_bytes(_png((5, 5, 5)))
    assert _cached_for_key.cache_info().misses == 0


def test_empty_bytes_yield_nothing() -> None:
    assert blurhash_for_bytes(b"", "etag-1") is None


# ---- matching Jellyfin's component counts ------------------------------------------

@pytest.mark.parametrize(
    "width,height,expected",
    [
        (600, 600, (5, 5)),      # a square album cover
        (1000, 1500, (4, 5)),    # a portrait poster
        (1280, 720, (6, 4)),     # a 16:9 backdrop
        (790, 536, (5, 4)),      # a logo
        (3840, 2160, (6, 4)),    # 16:9 again, at a different resolution
    ],
)
def test_the_component_counts_are_the_ones_jellyfin_picks(width, height, expected):
    """Taken from the hashes a live Jellyfin 12 had already stored.

    The counts are encoded in the hash itself, so a client decoding ours must see the
    same shape Jellyfin would have given it. Each case above was read back out of that
    server's BaseItemImageInfos table and its stored hash length matches what these
    counts produce, which is how the rule was established rather than guessed.
    """
    from infrastructure.images.blurhash import components_for_dimensions

    assert components_for_dimensions(width, height) == expected


@pytest.mark.parametrize("width,height", [(0, 100), (100, 0), (-1, 5)])
def test_unusable_dimensions_fall_back_rather_than_dividing_by_zero(width, height):
    from infrastructure.images.blurhash import components_for_dimensions

    assert components_for_dimensions(width, height) == (4, 4)


def test_a_very_wide_image_is_capped_at_nine_components():
    """Jellyfin caps both counts at 9; base83 cannot express more."""
    from infrastructure.images.blurhash import components_for_dimensions

    x, y = components_for_dimensions(10000, 100)

    assert x == 9 and 1 <= y <= 9


def test_a_square_cover_is_hashed_with_twenty_five_components():
    """The end-to-end consequence: our covers now carry Jellyfin-shaped hashes."""
    from infrastructure.images.blurhash import encode_image_bytes

    hashed = encode_image_bytes(_png((120, 60, 200), size=64))

    assert len(hashed) == 6 + 2 * (5 * 5 - 1)


def test_the_measured_size_is_the_source_size_not_the_sample_size():
    """The dimensions decide the components, so they must describe the original."""
    from infrastructure.images.blurhash import encode_image_bytes_with_size

    hashed, width, height = encode_image_bytes_with_size(_png((10, 20, 30), size=500))

    assert (width, height) == (500, 500)
    assert hashed
