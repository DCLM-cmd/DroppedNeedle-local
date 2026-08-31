"""BlurHash encoding for the images the compat APIs serve.

Jellyfin ships a blurhash for every image it knows about, and clients rely on it for
two things: the blurred placeholder shown while the real image loads, and - the part
that matters here - DE-DUPLICATING image downloads. Finamp warns the user outright
when a server returns none ("the server seems to be misconfigured and does not
compute blurhashes"), and then re-downloads artwork it already holds.

Implemented directly rather than pulled in as a dependency: the algorithm is a small,
frozen spec (https://blurha.sh), the reference implementations lean on numpy, and
Pillow - which we already ship - covers everything needed to read and downscale the
source image.

Encoding cost is bounded by ``_SAMPLE_SIZE``, not by the source image: a cover is
downscaled to at most 32x32 first, so one encode is a few thousand float operations
regardless of whether the original is 500px or 3000px.
"""

from __future__ import annotations

import logging
import math
import struct
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE83 = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"
)
# The fallback when the source dimensions are unknown. Jellyfin picks the component
# counts per image instead - see components_for_dimensions.
_COMPONENTS_X = 4
_COMPONENTS_Y = 4
# Jellyfin aims for roughly 16 near-square tiles per image.
_TARGET_TILES = 16
# The source is downscaled to this box before encoding. The transform integrates over
# the whole image, so a small sample changes the result only marginally.
_SAMPLE_SIZE = 32


def _base83(value: int, length: int) -> str:
    digits = []
    for i in range(1, length + 1):
        digit = (value // (83 ** (length - i))) % 83
        digits.append(_BASE83[digit])
    return "".join(digits)


def _srgb_to_linear(value: int) -> float:
    v = value / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(value: float) -> int:
    v = max(0.0, min(1.0, value))
    if v <= 0.0031308:
        return int(v * 12.92 * 255 + 0.5)
    return int((1.055 * (v ** (1 / 2.4)) - 0.055) * 255 + 0.5)


def _sign_pow(value: float, exponent: float) -> float:
    return math.copysign(abs(value) ** exponent, value)


def _encode_dc(rgb: tuple[float, float, float]) -> int:
    r, g, b = (_linear_to_srgb(c) for c in rgb)
    return (r << 16) + (g << 8) + b


def _encode_ac(rgb: tuple[float, float, float], maximum: float) -> int:
    quantised = [
        max(0, min(18, int(math.floor(_sign_pow(c / maximum, 0.5) * 9 + 9.5))))
        for c in rgb
    ]
    return quantised[0] * 19 * 19 + quantised[1] * 19 + quantised[2]


def _as_float32(value: float) -> float:
    """Round to IEEE single precision, the width Jellyfin's MathF calls work in."""
    return struct.unpack("f", struct.pack("f", value))[0]


def components_for_dimensions(width: int, height: int) -> tuple[int, int]:
    """The component counts Jellyfin would pick for an image of this size.

    Verified against a live Jellyfin 12 instance: every one of its stored hashes has
    exactly the length this reproduces. Jellyfin keeps the tiles as close to square as
    it can while staying near 16 of them, so the counts follow the aspect ratio - a
    square cover gets 5x5, a 16:9 backdrop 6x4. Matching it matters because the
    component counts are encoded in the hash, so a client decoding ours has to see the
    same shape it would get from Jellyfin.

    Mirrors ImageProcessor.GetImageBlurHash in the Jellyfin source, including its
    truncation towards zero and its cap of 9.
    """
    if width <= 0 or height <= 0:
        return _COMPONENTS_X, _COMPONENTS_Y
    # Jellyfin computes both in SINGLE precision (MathF.Sqrt on floats). Doing it in
    # double here would disagree with it whenever the true value sits within a float32
    # ulp of an integer: the cast truncates, so 3.9999998 and 4.0000001 pick different
    # component counts, and the count is encoded in the hash itself.
    components_x = _as_float32(math.sqrt(_as_float32(_TARGET_TILES * width / height)))
    components_y = _as_float32(components_x * height / width)
    return min(int(components_x) + 1, 9), min(int(components_y) + 1, 9)


def encode(
    pixels: list[tuple[int, int, int]],
    width: int,
    height: int,
    *,
    components_x: int = _COMPONENTS_X,
    components_y: int = _COMPONENTS_Y,
) -> str:
    """The blurhash of a row-major RGB pixel buffer.

    ``pixels`` is ``width * height`` ``(r, g, b)`` triples, each channel 0-255.
    """
    if width <= 0 or height <= 0 or len(pixels) != width * height:
        raise ValueError("pixel buffer does not match the given dimensions")
    if not (1 <= components_x <= 9 and 1 <= components_y <= 9):
        raise ValueError("blurhash components must each be between 1 and 9")

    # Linearise once: the transform reads every pixel components_x*components_y times.
    linear = [
        (_srgb_to_linear(p[0]), _srgb_to_linear(p[1]), _srgb_to_linear(p[2]))
        for p in pixels
    ]
    # Cosine tables, likewise - recomputing them inside the loop dominates the cost.
    cos_x = [
        [math.cos(math.pi * x * i / width) for i in range(width)]
        for x in range(components_x)
    ]
    cos_y = [
        [math.cos(math.pi * y * j / height) for j in range(height)]
        for y in range(components_y)
    ]

    scale = 1.0 / (width * height)
    components: list[tuple[float, float, float]] = []
    for y in range(components_y):
        row_cos = cos_y[y]
        for x in range(components_x):
            col_cos = cos_x[x]
            normalisation = 1.0 if (x == 0 and y == 0) else 2.0
            r = g = b = 0.0
            for j in range(height):
                base_j = row_cos[j]
                offset = j * width
                for i in range(width):
                    basis = base_j * col_cos[i]
                    pr, pg, pb = linear[offset + i]
                    r += basis * pr
                    g += basis * pg
                    b += basis * pb
            factor = normalisation * scale
            components.append((r * factor, g * factor, b * factor))

    dc, ac = components[0], components[1:]

    if ac:
        actual_max = max(max(abs(c) for c in triple) for triple in ac)
        quantised_max = max(0, min(82, int(math.floor(actual_max * 166 - 0.5))))
        maximum = (quantised_max + 1) / 166
    else:
        quantised_max = 0
        maximum = 1.0

    size_flag = (components_x - 1) + (components_y - 1) * 9
    out = _base83(size_flag, 1) + _base83(quantised_max, 1) + _base83(_encode_dc(dc), 4)
    for triple in ac:
        out += _base83(_encode_ac(triple, maximum), 2)
    return out


def encode_image_bytes_with_size(data: bytes) -> tuple[str, int, int] | None:
    """The blurhash of an encoded image together with the source's real dimensions.

    The dimensions are the ORIGINAL ones, not the sample's: they choose the component
    counts, and they are worth keeping because Jellyfin persists width and height on
    the same record as the hash.

    Artwork is best-effort everywhere else in this codebase and stays best-effort
    here: a cover we cannot decode simply gets no hash, never an error.
    """
    if not data:
        return None
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            source_width, source_height = image.size
            if source_width <= 0 or source_height <= 0:
                return None
            components_x, components_y = components_for_dimensions(
                source_width, source_height
            )
            image = image.convert("RGB")
            image.thumbnail((_SAMPLE_SIZE, _SAMPLE_SIZE))
            width, height = image.size
            if width <= 0 or height <= 0:
                return None
            # getdata() is deprecated in Pillow 14; get_flattened_data() is its
            # replacement and is not in older releases, so prefer it when present.
            reader = getattr(image, "get_flattened_data", None) or image.getdata
            hashed = encode(
                list(reader()),
                width,
                height,
                components_x=components_x,
                components_y=components_y,
            )
            return hashed, source_width, source_height
    except Exception:  # noqa: BLE001 - artwork is decorative; never fail a listing
        logger.debug("Could not compute a blurhash for an image", exc_info=True)
        return None


def encode_image_bytes(data: bytes) -> str | None:
    """The blurhash of an encoded image (JPEG/PNG/...), or None if it cannot be read."""
    measured = encode_image_bytes_with_size(data)
    return None if measured is None else measured[0]


@lru_cache(maxsize=2048)
def _cached_for_key(cache_key: str, data: bytes) -> str | None:
    del cache_key  # identity only: it is what makes the memo hit
    return encode_image_bytes(data)


def blurhash_for_bytes(data: bytes, cache_key: str | None = None) -> str | None:
    """The blurhash of an in-memory image, memoised on ``cache_key``.

    The key is the image's content hash, which callers already hold (it is the same
    value they publish as the image's ETag). Without it every listing re-encoded every
    cover from scratch: a 100-album page took 5.3 SECONDS, on every single request,
    because only the path-based entry point below was memoised and the local-artwork
    path went through here.

    Passing no key skips the memo rather than caching under a wrong one.
    """
    if not data:
        return None
    if cache_key is None:
        return encode_image_bytes(data)
    return _cached_for_key(cache_key, data)


@lru_cache(maxsize=2048)
def _cached_for_path(path: str, mtime_ns: int, size: int) -> str | None:
    del mtime_ns, size  # identity only: they make the cache key change with the file
    try:
        return encode_image_bytes(Path(path).read_bytes())
    except OSError:
        return None


def blurhash_for_file(path: Path | str) -> str | None:
    """The blurhash of a cached image file, memoised on (path, mtime, size).

    Listings ask for the same covers over and over, and the encode is the expensive
    part, so the result is kept per file identity - replacing the file re-encodes.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return _cached_for_path(str(path), stat.st_mtime_ns, stat.st_size)
