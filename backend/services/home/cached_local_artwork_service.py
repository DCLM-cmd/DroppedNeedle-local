"""Read only artwork bytes already managed by the local catalog."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from infrastructure.audio.tagger import AudioTagger
from repositories.coverart_disk_cache import get_cache_filename

if TYPE_CHECKING:
    from infrastructure.persistence.native_library_store import NativeLibraryStore


def _content_type(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


class CachedLocalArtworkService:
    """Resolve associations without provider calls, warming, or arbitrary file reads."""

    def __init__(self, store: "NativeLibraryStore", cache_dir: Path) -> None:
        self._store = store
        self._cache_dir = cache_dir.resolve()
        self._tagger = AudioTagger()

    def _managed_path(self, value: str) -> Path | None:
        candidate = Path(value)
        path = candidate if candidate.is_absolute() else self._cache_dir / candidate
        resolved = path.resolve()
        if not resolved.is_relative_to(self._cache_dir):
            return None
        return resolved

    def _cache_paths(self, context: dict[str, Any]) -> list[Path]:
        paths: list[Path] = []
        locator = context.get("source_locator")
        if isinstance(locator, str) and locator:
            direct = self._managed_path(locator)
            if direct is not None and direct.suffix == ".bin":
                paths.append(direct)
        identifiers: list[str] = []
        provider_id = context.get("provider_id")
        if isinstance(provider_id, str) and provider_id:
            identifiers.append(f"rg_{provider_id}")
        if isinstance(locator, str) and locator:
            identifiers.append(
                locator if locator.startswith("rg_") else f"rg_{locator}"
            )
        for identifier in dict.fromkeys(identifiers):
            for suffix in ("500", "250", "1200", "orig"):
                paths.append(
                    self._cache_dir / f"{get_cache_filename(identifier, suffix)}.bin"
                )
        return list(dict.fromkeys(paths))

    async def read(self, context: dict[str, Any]) -> tuple[bytes, str, str, str] | None:
        source = str(context.get("source") or "")
        if source == "embedded":
            if context.get("embedded_file_availability") != "indexed":
                return None
            file_path = context.get("embedded_file_path")
            if not isinstance(file_path, str) or not file_path:
                return None
            content = await asyncio.to_thread(
                self._tagger.read_cover_art, Path(file_path)
            )
            content_type = _content_type(content or b"")
            if not content or content_type is None:
                return None
            return (
                content,
                content_type,
                "embedded",
                hashlib.sha1(content).hexdigest(),
            )

        if source not in {"provider", "cover_cache", "manual"}:
            return None
        for path in self._cache_paths(context):
            try:
                content = await asyncio.to_thread(path.read_bytes)
            except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
                continue
            content_type = _content_type(content)
            if content_type is not None:
                return (
                    content,
                    content_type,
                    source,
                    hashlib.sha1(content).hexdigest(),
                )
        return None

    async def read_identity(self, context: dict[str, Any]) -> str | None:
        """The artwork's content hash, without reading the artwork.

        Listings need this - it is the image's ETag, and the key the stored blurhash
        hangs on - but they do not need the picture. Deriving it through ``read`` meant
        every listed album's cover was loaded in full and re-hashed: 181 MB of image
        I/O for one 100-album page, paid twice over because the ETag and the blurhash
        each asked separately. Jellyfin never touches an image file to serve a listing;
        it keeps the identity on the item record.

        The value is byte-for-byte the one ``read`` returns. For cached artwork the
        disk cache already recorded it at write time, so this is a small JSON read; the
        candidates are walked in ``read``'s order and the first usable one wins, so
        both paths agree on WHICH image an album's identity comes from.
        """
        source = str(context.get("source") or "")
        if source == "embedded":
            # Nothing is cached beside an audio file, so the tag still has to be read;
            # memoised on the file's identity, which is what makes it cheap on repeat.
            resolved = await self.read(context)
            return None if resolved is None else resolved[3]
        if source not in {"provider", "cover_cache", "manual"}:
            return None
        for path in self._cache_paths(context):
            digest = await self._identity_of(path)
            if digest is not None:
                return digest
        return None

    async def _identity_of(self, path: Path) -> str | None:
        """The stored content hash of one cached image, or None if it is not usable."""

        def read_meta() -> str | None:
            if not path.exists():
                return None
            meta_path = path.with_suffix(".meta.json")
            try:
                raw = meta_path.read_text()
            except (OSError, ValueError):
                return None
            try:
                meta = json.loads(raw)
            except ValueError:
                return None
            if not isinstance(meta, dict):
                return None
            digest = meta.get("content_sha1")
            content_type = meta.get("content_type")
            # Only trust the recorded hash when the entry also looks like the image
            # ``read`` would have accepted, so the two never disagree.
            if not isinstance(digest, str) or not digest:
                return None
            if isinstance(content_type, str) and not content_type.startswith("image/"):
                return None
            return digest

        digest = await asyncio.to_thread(read_meta)
        if digest is not None:
            return digest
        # No usable metadata - fall back to what read() does, so an entry written
        # before the hash was recorded still resolves rather than being skipped.
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            return None
        if _content_type(content) is None:
            return None
        return hashlib.sha1(content).hexdigest()

    async def get(
        self, album_id: str, cover_version: int
    ) -> tuple[bytes, str, str, str] | None:
        context = await self._store.get_cached_local_artwork_context(
            album_id, cover_version
        )
        return await self.read(context) if context is not None else None
