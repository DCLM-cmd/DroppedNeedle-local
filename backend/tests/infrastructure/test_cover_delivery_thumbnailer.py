from io import BytesIO

import pytest
from PIL import Image

from infrastructure.images.cover_delivery_thumbnailer import CoverDeliveryThumbnailer


def _image_bytes(
    image_format: str,
    size: tuple[int, int],
    mode: str = "RGB",
) -> bytes:
    image = Image.new(mode, size, (30, 90, 150, 128) if mode == "RGBA" else "navy")
    output = BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _dimensions(content: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(content)) as image:
        return image.size


@pytest.mark.asyncio
async def test_downsizes_oversized_jpeg_to_requested_bound():
    content = _image_bytes("JPEG", (1200, 800))
    thumbnailer = CoverDeliveryThumbnailer()

    prepared, content_type = await thumbnailer.prepare(content, "image/jpeg", 250)

    assert _dimensions(prepared) == (250, 167)
    assert len(prepared) < len(content)
    assert content_type == "image/jpeg"


@pytest.mark.asyncio
async def test_downsizes_transparent_png_without_losing_alpha():
    content = _image_bytes("PNG", (400, 600), mode="RGBA")
    thumbnailer = CoverDeliveryThumbnailer()

    prepared, content_type = await thumbnailer.prepare(content, "image/png", 250)

    with Image.open(BytesIO(prepared)) as image:
        assert image.size == (167, 250)
        assert image.mode == "RGBA"
    assert content_type == "image/png"


@pytest.mark.asyncio
async def test_preserves_already_sized_image_bytes():
    content = _image_bytes("WEBP", (200, 150))
    thumbnailer = CoverDeliveryThumbnailer()

    prepared, content_type = await thumbnailer.prepare(content, "image/webp", 250)

    assert prepared is content
    assert content_type == "image/webp"


@pytest.mark.asyncio
async def test_preserves_original_size_and_unsupported_payloads():
    content = _image_bytes("JPEG", (800, 800))
    invalid = b"not-an-image"
    thumbnailer = CoverDeliveryThumbnailer()

    original, original_type = await thumbnailer.prepare(content, "image/jpeg", None)
    unchanged, unchanged_type = await thumbnailer.prepare(invalid, "image/jpeg", 250)

    assert original is content
    assert original_type == "image/jpeg"
    assert unchanged is invalid
    assert unchanged_type == "image/jpeg"


@pytest.mark.asyncio
async def test_reuses_processed_thumbnail_for_same_content_and_size():
    content = _image_bytes("JPEG", (1000, 1000))
    thumbnailer = CoverDeliveryThumbnailer()

    first, _ = await thumbnailer.prepare(content, "image/jpeg", 250)
    second, _ = await thumbnailer.prepare(content, "image/jpeg", 250)

    assert second is first
