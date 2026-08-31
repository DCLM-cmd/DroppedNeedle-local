"""What the organizer does once a rescan has finished.

A scan tells the catalog what is on disk. It does not tidy up after the ways a
library gets untidy, and three of those show up over and over here:

* the same song present twice, because the album arrived again in another format;
* one album split across several catalog rows, so half of it looks missing;
* folders left behind holding nothing that plays.

Each pass is independent and best-effort. A scan that found the music must never be
reported as failed because the tidying afterwards hit a permission error, and one
pass failing must not stop the next.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from services.native.quality_tiers import tier_for, tier_rank

if TYPE_CHECKING:
    from infrastructure.persistence.native_library_store import NativeLibraryStore
    from services.native.catalog_identity_hygiene_service import (
        CatalogIdentityHygieneService,
    )

logger = logging.getLogger(__name__)

# Bounds so one pass can never become an unbounded job on a large library; whatever is
# left is picked up by the next scan.
_MAX_DUPLICATE_GROUPS = 500
_MAX_REMOVED_DIRECTORIES = 2000
# Only these count as content. A folder holding just artwork, a playlist file or a log
# is an empty album folder as far as a music library is concerned.
_AUDIO_SUFFIXES = frozenset(
    {".flac", ".mp3", ".m4a", ".m4b", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".wma",
     ".aac", ".alac", ".aiff", ".aif", ".ape", ".wv", ".dsf", ".dff"}
)
# Never touched: the recycle bin holds what earlier passes deliberately preserved, and
# dot-directories belong to whatever put them there.
_PROTECTED_DIRECTORY_NAMES = frozenset({".recycle", "@eaDir", "lost+found"})
# Used only when nothing supplies the configured value; the real one comes from
# settings, because how long a wrong upgrade stays recoverable is the user's call.
_DEFAULT_RECYCLE_RETENTION_DAYS = 30


def _quality_rank(row: dict[str, Any]) -> tuple[int, int, int]:
    """How good this copy is. Higher sorts better.

    Tier first, exactly as the scanner and the import gate judge quality, so "keep the
    best" here means the same thing it means everywhere else - a FLAC beats an MP3
    whatever its bitrate. Bitrate breaks a tie inside a tier, and size breaks a tie
    after that, so two lossless copies of different depth resolve deterministically
    rather than by row order.
    """
    tier = tier_for(str(row.get("file_format") or ""), row.get("bit_rate"))
    return (
        tier_rank(tier),
        int(row.get("bit_rate") or 0),
        int(row.get("file_size_bytes") or 0),
    )


class LibraryHousekeepingService:
    """Deduplication, album merging and folder cleanup, run after a scan."""

    def __init__(
        self,
        store: "NativeLibraryStore",
        *,
        hygiene: "CatalogIdentityHygieneService | None" = None,
        recycle_bin: "Path | Callable[[], Path | None] | None" = None,
        library_roots: Callable[[], list[Path]] | None = None,
        recycle_retention_days: Callable[[], int] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._hygiene = hygiene
        self._recycle_bin = recycle_bin
        self._library_roots = library_roots
        self._recycle_retention_days = recycle_retention_days
        self._clock = clock

    async def run_after_scan(self) -> dict[str, int]:
        """All three passes. Never raises: a scan is not undone by untidy shelves."""
        counts = {"deduplicated": 0, "merged": 0, "folders_removed": 0}
        counts["recycled_removed"] = 0
        counts["rows_purged"] = 0
        counts["tracks_rehomed"] = 0
        for name, pass_ in (
            ("tracks_rehomed", self.rehome_retired_tracks),
            ("deduplicated", self.deduplicate),
            ("merged", self.merge_split_albums),
            ("rows_purged", self.purge_superseded_rows),
            ("recycled_removed", self.empty_recycle_bin),
            ("folders_removed", self.remove_empty_folders),
        ):
            try:
                counts[name] = await pass_()
            except Exception:  # noqa: BLE001 - tidying never fails the scan
                logger.warning("Post-scan %s pass failed", name, exc_info=True)
        if any(counts.values()):
            logger.info("Post-scan housekeeping: %s", counts)
        return counts

    # ---- tracks left on a merged-away album row -----------------------------------

    async def rehome_retired_tracks(self) -> int:
        """Move tracks off retired album rows onto the album that survived.

        Retiring an album moves the ROW; anything still hanging off it goes invisible,
        because listings follow the survivor. One album kept ten of its eleven tracks
        on the retired half and showed a single song - every file present, every row
        indexed, and the library reporting one.

        Runs FIRST: deduplication compares copies within an album, so the tracks have
        to be on the right one before anything is compared.
        """
        moved = await self._store.rehome_tracks_from_retired_albums()
        if moved:
            logger.info("housekeeping.tracks_rehomed", extra={"tracks": moved})
        return moved

    # ---- the same song twice --------------------------------------------------

    async def deduplicate(self) -> int:
        """Keep the best copy of each song and retire the rest.

        "Best" is the quality tier, so a FLAC always wins over an MP3 of the same
        track - which is the point of holding both only briefly. The loser's bytes go
        to the recycle bin rather than being deleted: an upgrade that turns out to
        have been wrong must stay recoverable.
        """
        groups = await self._store.list_duplicate_track_positions(
            limit=_MAX_DUPLICATE_GROUPS
        )
        retired = 0
        for group in groups:
            ranked = sorted(group, key=_quality_rank, reverse=True)
            keeper, losers = ranked[0], ranked[1:]
            for loser in losers:
                if str(loser.get("file_path")) == str(keeper.get("file_path")):
                    continue
                if await self._retire(loser, keeper):
                    retired += 1
        return retired

    async def _retire(self, loser: dict[str, Any], keeper: dict[str, Any]) -> bool:
        path = str(loser.get("file_path") or "")
        if not path:
            return False
        moved = await self._recycle(Path(path))
        if moved is None:
            return False
        # The row follows its file into the bin. Left pointing at the library path it
        # no longer occupies, it reads as a second copy of the song that is merely
        # missing - which is what made an album look like it still held both, and what
        # blocked later imports from taking the slot.
        await self._store.retire_duplicate_track(
            str(loser["id"]), recycled_path=str(moved), now=self._clock()
        )
        logger.info(
            "housekeeping.duplicate_retired",
            extra={
                "album": loser.get("album_title"),
                "track": loser.get("track_number"),
                "kept": keeper.get("file_format"),
                "retired": loser.get("file_format"),
            },
        )
        return True

    async def _recycle(self, path: Path) -> Path | None:
        """Move a file into the recycle bin, returning where it went.

        None when it cannot be preserved: without a bin the file stays exactly where
        it is, since deleting the only copy of something to tidy up would be a worse
        outcome than the untidiness.
        """
        bin_path = (
            self._recycle_bin() if callable(self._recycle_bin) else self._recycle_bin
        )
        if bin_path is None:
            return None

        def move() -> Path | None:
            if not path.is_file():
                return None
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(self._clock()))
            destination = bin_path / f"{stamp}-duplicates" / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            target = destination
            suffix = 1
            while target.exists():
                target = destination.with_name(
                    f"{destination.stem}-{suffix}{destination.suffix}"
                )
                suffix += 1
            shutil.move(str(path), str(target))
            return target

        try:
            return await asyncio.to_thread(move)
        except OSError:
            logger.warning("Could not recycle %s", path, exc_info=True)
            return None

    # ---- one album, several catalog rows ---------------------------------------

    async def merge_split_albums(self) -> int:
        """Fold trackless album rows into the row that holds the music.

        The catalog already knows how to do this safely - it only merges a row with no
        tracks into exactly one same-titled sibling that has them, and refuses when
        anything about the pair is ambiguous. This just makes a scan ask for it.
        """
        if self._hygiene is None:
            return 0
        job = await self._hygiene.enqueue_backfill()
        return int(job.get("expected_work_count") or 0)

    # ---- rows for files a better copy replaced -------------------------------------

    async def purge_superseded_rows(self) -> int:
        """Take replaced copies out of the catalog.

        A scan marks a file that has left the disk as ``missing`` and stops there, so
        every replaced copy leaves its row behind. They are invisible in listings but
        not harmless: a row sitting at a destination is what a later import trips over.

        A missing row with no live copy at its position is left alone - that one is the
        record that an album lost a track, which the library is meant to keep.
        """
        purged = await self._store.purge_superseded_track_rows()
        if any(purged.values()):
            logger.info("housekeeping.superseded_rows_purged: %s", purged)
        return int(purged.get("removed", 0))

    # ---- what the bin is still holding -------------------------------------------

    async def empty_recycle_bin(self) -> int:
        """Delete recycled files past their retention and un-file them from the catalog.

        Two halves, and only one existed. Files were pruned on a timer, but the catalog
        rows naming them stayed - still pointing into the bin, still counted as part of
        the album they had been removed from, which is enough to make an album read as
        holding a track it no longer has.

        Retention is the user's setting and is honoured: what is inside the window is
        what makes a wrong upgrade recoverable, and emptying that early would throw the
        safety net away. Returns how many entries were deleted.
        """
        bin_path = (
            self._recycle_bin() if callable(self._recycle_bin) else self._recycle_bin
        )
        if bin_path is None:
            return 0
        # The catalog is cleared FIRST: a row still naming a file is at least
        # consistent, while a row naming bytes that have just been deleted is not.
        try:
            purged = await self._store.purge_recycled_track_rows(str(bin_path))
            if any(purged.values()):
                logger.info("housekeeping.recycled_rows_cleared: %s", purged)
        except Exception:  # noqa: BLE001 - tidying never fails a scan
            logger.warning("Could not clear recycled catalog rows", exc_info=True)
        retention = (
            self._recycle_retention_days()
            if self._recycle_retention_days is not None
            else _DEFAULT_RECYCLE_RETENTION_DAYS
        )
        from services.native.recycle_bin import prune

        removed = await asyncio.to_thread(prune, Path(bin_path), max(0, retention))
        if removed:
            logger.info(
                "housekeeping.recycle_bin_pruned",
                extra={"entries": removed, "retention_days": retention},
            )
        return removed

    # ---- folders with nothing that plays ----------------------------------------

    async def remove_empty_folders(self) -> int:
        """Remove directories under the library roots that hold no audio.

        Deepest first, so an album folder emptied by this pass lets its artist folder
        go too. A folder holding only artwork or a stray log is empty for a music
        library's purposes, and its leftovers go with it - but only ever inside a
        library root, and never a protected directory.
        """
        roots = self._library_roots() if self._library_roots is not None else []
        if not roots:
            return 0

        def sweep() -> int:
            removed = 0
            for root in roots:
                resolved = Path(root).resolve()
                if not resolved.is_dir():
                    continue
                for directory, _subdirs, _files in sorted(
                    os.walk(resolved, topdown=False), key=lambda item: -len(item[0])
                ):
                    if removed >= _MAX_REMOVED_DIRECTORIES:
                        return removed
                    current = Path(directory)
                    if current == resolved:
                        continue
                    if any(
                        part in _PROTECTED_DIRECTORY_NAMES or part.startswith(".")
                        for part in current.relative_to(resolved).parts
                    ):
                        continue
                    if self._holds_audio(current):
                        continue
                    try:
                        shutil.rmtree(current)
                        removed += 1
                    except OSError:
                        logger.warning(
                            "Could not remove the empty folder %s", current,
                            exc_info=True,
                        )
            return removed

        return await asyncio.to_thread(sweep)

    @staticmethod
    def _holds_audio(directory: Path) -> bool:
        for current, _subdirs, files in os.walk(directory):
            del current
            for name in files:
                if Path(name).suffix.casefold() in _AUDIO_SUFFIXES:
                    return True
        return False
