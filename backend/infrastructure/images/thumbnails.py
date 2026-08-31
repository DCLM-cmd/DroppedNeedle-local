"""Serving artwork at the size a client asked for.

Jellyfin resizes: a request for a 300px thumbnail returns a 300px thumbnail, and its
clients rely on that. Finamp asks for small images when it draws a grid of covers and
for large ones only when a cover fills the screen.

DroppedNeedle used to return whatever was cached, at full size, whatever was asked -
a 484 KB cover for a 300px tile. A screen of a hundred albums therefore pulled tens of
megabytes over the network, which on a phone reads as covers that load slowly or not
at all. Measured against the Jellyfin instance next door, the same request there costs
49 KB where ours cost 484.

The resize is memoised on the source image's content hash and the target size, so it
happens once per image per size rather than once per request. That memo is the whole
reason this is cheap enough to do inline: unlike a blurhash, one downscale is a few
milliseconds, and after the first request it is a dictionary lookup.
"""

from __future__ import annotations

import hashlib
import io
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# Below this there is nothing worth re-encoding: the transfer saved is smaller than
# the CPU spent, and re-encoding a small JPEG mostly makes it worse.
_MIN_TARGET = 32
# JPEG quality for what we produce. Jellyfin's clients ask for 90-96; this sits in the
# same place and is visually lossless at thumbnail sizes.
_QUALITY = 90


@lru_cache(maxsize=4096)
def _cached_resize(
    content_sha1: str, target_px: int, data: bytes
) -> tuple[bytes, str] | None:
    del content_sha1  # identity only: it is what makes the memo hit
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                return None
            if max(width, height) <= target_px:
                return None  # already small enough; sending it untouched is correct
            # Keep PNG output for anything a JPEG cannot carry, so a transparent
            # logo does not come back with a black box behind it. RGBA and LA hold
            # their alpha in a channel; palette and greyscale images declare it in
            # the transparency key instead, so both have to be checked.
            keeps_alpha = image.mode in ("RGBA", "LA") or "transparency" in (
                image.info or {}
            )
            image.thumbnail((target_px, target_px), Image.LANCZOS)
            buffer = io.BytesIO()
            if keeps_alpha:
                image.convert("RGBA").save(buffer, format="PNG", optimize=True)
                return buffer.getvalue(), "image/png"
            image.convert("RGB").save(
                buffer, format="JPEG", quality=_QUALITY, optimize=True
            )
            return buffer.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - artwork is decorative; fall back to the original
        logger.debug("Could not resize an image to %dpx", target_px, exc_info=True)
        return None


def resize_to_fit(
    data: bytes, content_type: str, target_px: int | None
) -> tuple[bytes, str]:
    """The image scaled to fit ``target_px`` on its longest side.

    Returns the original bytes unchanged when no size was asked for, the image is
    already small enough, or it cannot be decoded - artwork is decorative and a client
    that gets a slightly-too-large picture is far better off than one that gets none.
    """
    if not data or target_px is None or target_px < _MIN_TARGET:
        return data, content_type
    resized = _cached_resize(
        hashlib.sha1(data).hexdigest(), int(target_px), data
    )
    if resized is None:
        return data, content_type
    return resized
