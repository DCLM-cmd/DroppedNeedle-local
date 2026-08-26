"""Bounded cover thumbnails for HTTP delivery."""

from __future__ import annotations

import asyncio
import hashlib
import warnings
from collections import OrderedDict
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_DELIVERY_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DELIVERY_IMAGE_PIXELS = 40_000_000
DELIVERY_CACHE_MAX_ENTRIES = 256
DELIVERY_CACHE_MAX_BYTES = 16 * 1024 * 1024

_SUPPORTED_FORMATS = {
    "JPEG": ("JPEG", "image/jpeg"),
    "PNG": ("PNG", "image/png"),
    "WEBP": ("WEBP", "image/webp"),
}


class CoverDeliveryThumbnailer:
    """Downsize oversized raster covers without changing source-selection caches."""

    def __init__(
        self,
        *,
        max_entries: int = DELIVERY_CACHE_MAX_ENTRIES,
        max_bytes: int = DELIVERY_CACHE_MAX_BYTES,
    ) -> None:
        self._max_entries = max(1, max_entries)
        self._max_bytes = max(1, max_bytes)
        self._entries: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._entry_bytes = 0
        self._already_sized: OrderedDict[str, None] = OrderedDict()
        self._lock = asyncio.Lock()

    async def prepare(
        self,
        content: bytes,
        content_type: str,
        maximum_size: int | None,
    ) -> tuple[bytes, str]:
        if maximum_size is None or maximum_size <= 0:
            return content, content_type

        base_type = content_type.partition(";")[0].strip().casefold()
        if base_type not in {"image/jpeg", "image/jpg", "image/png", "image/webp"}:
            return content, content_type
        if not content or len(content) > MAX_DELIVERY_IMAGE_BYTES:
            return content, content_type

        key = f"{maximum_size}:{hashlib.sha1(content).hexdigest()}"
        async with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
            if key in self._already_sized:
                self._already_sized.move_to_end(key)
                return content, content_type

        prepared = await asyncio.to_thread(
            self._prepare_sync,
            content,
            content_type,
            maximum_size,
        )
        if prepared is None:
            async with self._lock:
                self._already_sized[key] = None
                self._already_sized.move_to_end(key)
                while len(self._already_sized) > self._max_entries:
                    self._already_sized.popitem(last=False)
            return content, content_type

        prepared_content, prepared_type = prepared
        if len(prepared_content) > self._max_bytes:
            return prepared_content, prepared_type

        async with self._lock:
            existing = self._entries.pop(key, None)
            if existing is not None:
                self._entry_bytes -= len(existing[0])
            self._entries[key] = prepared
            self._entries.move_to_end(key)
            self._entry_bytes += len(prepared_content)
            while self._entries and (
                len(self._entries) > self._max_entries
                or self._entry_bytes > self._max_bytes
            ):
                _, (evicted_content, _) = self._entries.popitem(last=False)
                self._entry_bytes -= len(evicted_content)
        return prepared

    @staticmethod
    def _prepare_sync(
        content: bytes,
        content_type: str,
        maximum_size: int,
    ) -> tuple[bytes, str] | None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as source:
                    image_format = source.format
                    width, height = source.size
                    if (
                        image_format not in _SUPPORTED_FORMATS
                        or width <= 0
                        or height <= 0
                        or width * height > MAX_DELIVERY_IMAGE_PIXELS
                        or max(width, height) <= maximum_size
                    ):
                        return None

                    source.load()
                    image = ImageOps.exif_transpose(source)
                    image.thumbnail(
                        (maximum_size, maximum_size),
                        Image.Resampling.LANCZOS,
                    )
                    pil_format, output_type = _SUPPORTED_FORMATS[image_format]
                    if pil_format == "JPEG" and image.mode not in ("RGB", "L"):
                        flattened = Image.new("RGB", image.size, "white")
                        if image.mode in ("RGBA", "LA"):
                            flattened.paste(image, mask=image.getchannel("A"))
                        else:
                            flattened.paste(image.convert("RGB"))
                        image = flattened

                    output = BytesIO()
                    save_options: dict[str, object] = {}
                    if pil_format == "JPEG":
                        save_options = {
                            "quality": 85,
                            "optimize": True,
                            "progressive": True,
                        }
                    elif pil_format == "PNG":
                        save_options = {"optimize": True}
                    else:
                        save_options = {"quality": 85, "method": 4}
                    image.save(output, format=pil_format, **save_options)
                    return output.getvalue(), output_type
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ):
            # Source fetchers already validate their provider responses. If an old
            # cache entry cannot be safely decoded, retain the established delivery
            # behavior instead of turning an available cover into a placeholder.
            return None
