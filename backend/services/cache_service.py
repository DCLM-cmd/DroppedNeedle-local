import asyncio
import logging
import os
import time
from pathlib import Path

from infrastructure.cache.memory_cache import CacheInterface
from infrastructure.cache.cache_metrics import InstrumentedCache
from infrastructure.cache.cache_keys import AUDIODB_PREFIX
from infrastructure.persistence import LibraryDB
from infrastructure.cache.disk_cache import DiskMetadataCache
from infrastructure.persistence.mb_response_store import MbResponseStore
from api.v1.schemas.cache import CacheStats, CacheClearResponse, CachePrefixStat

logger = logging.getLogger(__name__)


def get_covers_cache_dir() -> Path:
    from core.config import get_settings

    return get_settings().cache_dir / "covers"


class CacheService:
    def __init__(
        self,
        cache: CacheInterface,
        library_db: LibraryDB,
        disk_cache: DiskMetadataCache,
        mb_response_store: MbResponseStore,
    ):
        self._cache = cache
        self._library_db = library_db
        self._disk_cache = disk_cache
        self._mb_response_store = mb_response_store
        self._cached_stats: CacheStats | None = None
        self._stats_cache_time: float = 0.0
        self._stats_cache_ttl: float = 30.0
        self._stats_revision = 0
        self._stats_task: asyncio.Task[CacheStats] | None = None
        self._stats_task_revision = 0

    def _invalidate_stats(self) -> None:
        self._stats_revision += 1
        self._cached_stats = None

    @staticmethod
    def _scan_covers(directory: Path, *, delete: bool = False) -> tuple[int, int]:
        count = 0
        size = 0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_count, child_size = CacheService._scan_covers(
                                Path(entry.path), delete=delete
                            )
                            count += child_count
                            size += child_size
                        elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                            entry_size = entry.stat(follow_symlinks=False).st_size
                            if delete:
                                os.unlink(entry.path)
                            count += 1
                            size += entry_size
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            pass
        return count, size

    def _clear_genre_disk_cache(self) -> int:
        from core.config import get_settings

        directory = get_settings().cache_dir / "genre_sections"
        count = 0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.name.endswith(".json") or not entry.is_file(
                        follow_symlinks=False
                    ):
                        continue
                    try:
                        os.unlink(entry.path)
                        count += 1
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            pass
        return count

    async def get_stats(self) -> CacheStats:
        while True:
            if self._cached_stats is not None and (
                time.monotonic() - self._stats_cache_time
            ) < self._stats_cache_ttl:
                return self._cached_stats
            if self._stats_task is None or self._stats_task.done():
                self._stats_task = asyncio.create_task(self._refresh_stats())
                self._stats_task_revision = self._stats_revision
            revision = self._stats_task_revision
            stats = await asyncio.shield(self._stats_task)
            if revision == self._stats_revision:
                return stats

    async def _refresh_stats(self) -> CacheStats:
        revision = self._stats_revision
        covers_cache_dir = get_covers_cache_dir()
        try:
            memory_entries = self._cache.size()

            memory_hits = 0
            memory_misses = 0
            memory_hit_rate_percent = 0.0
            per_prefix: list[CachePrefixStat] = []
            counters_since: int | None = None
            if isinstance(self._cache, InstrumentedCache):
                observability = self._cache.observability()
                memory_hits = observability["memory_hits"]
                memory_misses = observability["memory_misses"]
                memory_hit_rate_percent = observability["memory_hit_rate_percent"]
                per_prefix = [
                    CachePrefixStat(**row) for row in observability["per_prefix"]
                ]
                counters_since = observability["counters_since"]

            memory_bytes = self._cache.estimate_memory_bytes()
            memory_mb = memory_bytes / (1024 * 1024)

            metadata_stats, response_stats, cover_stats = await asyncio.gather(
                asyncio.to_thread(self._disk_cache.get_stats),
                asyncio.to_thread(self._mb_response_store.stats),
                asyncio.to_thread(self._scan_covers, covers_cache_dir),
            )
            metadata_count = metadata_stats["total_count"]
            metadata_albums = metadata_stats["album_count"]
            metadata_artists = metadata_stats["artist_count"]

            disk_count, disk_bytes = cover_stats

            disk_mb = disk_bytes / (1024 * 1024)

            cache_stats_getter = getattr(self._library_db, "get_cache_stats", None)
            lib_stats = (
                await cache_stats_getter()
                if cache_stats_getter is not None
                else await self._library_db.get_stats()
            )
            lib_bytes = response_stats["database_allocated_bytes"]
            lib_mb = lib_bytes / (1024 * 1024)

            total_bytes = (
                memory_bytes
                + disk_bytes
                + metadata_stats["total_size_bytes"]
                + lib_bytes
                + response_stats["database_wal_bytes"]
            )
            total_mb = total_bytes / (1024 * 1024)

            stats = CacheStats(
                memory_entries=memory_entries,
                memory_size_bytes=memory_bytes,
                memory_size_mb=round(memory_mb, 2),
                disk_metadata_count=metadata_count,
                disk_metadata_albums=metadata_albums,
                disk_metadata_artists=metadata_artists,
                disk_cover_count=disk_count,
                disk_cover_size_bytes=disk_bytes,
                disk_cover_size_mb=round(disk_mb, 2),
                library_db_artist_count=lib_stats["artist_count"],
                library_db_album_count=lib_stats["album_count"],
                library_db_size_bytes=lib_bytes,
                library_db_size_mb=round(lib_mb, 2),
                library_db_last_sync=lib_stats.get("last_sync"),
                total_size_bytes=total_bytes,
                total_size_mb=round(total_mb, 2),
                response_entries=response_stats["response_entries"],
                response_logical_bytes=response_stats["response_logical_bytes"],
                response_hits=response_stats["response_hits"],
                response_evictions=response_stats["response_evictions"],
                response_speculative_used=response_stats["response_speculative_used"],
                database_allocated_bytes=lib_bytes,
                database_wal_bytes=response_stats["database_wal_bytes"],
                disk_audiodb_artist_count=metadata_stats.get("audiodb_artist_count", 0),
                disk_audiodb_album_count=metadata_stats.get("audiodb_album_count", 0),
                memory_hits=memory_hits,
                memory_misses=memory_misses,
                memory_hit_rate_percent=memory_hit_rate_percent,
                per_prefix=per_prefix,
                counters_since=counters_since,
            )

            if revision == self._stats_revision:
                self._cached_stats = stats
                self._stats_cache_time = time.monotonic()

            return stats
        except Exception:
            logger.exception("Failed to collect cache statistics")
            raise

    async def clear_memory_cache(self) -> CacheClearResponse:
        try:
            entries_before = self._cache.size()
            await self._cache.clear()


            return CacheClearResponse(
                success=True,
                message=f"Successfully cleared {entries_before} memory cache entries",
                cleared_memory_entries=entries_before,
                cleared_disk_files=0,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to clear memory cache: {e}")
            return CacheClearResponse(
                success=False,
                message=f"Failed to clear memory cache: {str(e)}",
                cleared_memory_entries=0,
                cleared_disk_files=0,
            )
        finally:
            self._invalidate_stats()

    async def clear_disk_cache(self) -> CacheClearResponse:
        covers_cache_dir = get_covers_cache_dir()
        try:
            metadata_stats = await asyncio.to_thread(self._disk_cache.get_stats)
            metadata_count = metadata_stats["total_count"]
            await self._disk_cache.clear_all()
            response_entries = await self._mb_response_store.clear()

            cover_files, _ = await asyncio.to_thread(
                self._scan_covers, covers_cache_dir, delete=True
            )
            genre_files = await asyncio.to_thread(self._clear_genre_disk_cache)

            return CacheClearResponse(
                success=True,
                message=f"Successfully cleared {metadata_count} metadata files, {cover_files} cover files, {genre_files} genre files and {response_entries} response entries",
                cleared_memory_entries=0,
                cleared_disk_files=cover_files + genre_files + metadata_count,
                cover_files_cleared=cover_files,
                cleared_response_entries=response_entries,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to clear disk cache: {e}")
            return CacheClearResponse(
                success=False,
                message=f"Failed to clear disk cache: {str(e)}",
                cleared_memory_entries=0,
                cleared_disk_files=0,
            )
        finally:
            self._invalidate_stats()

    async def clear_metadata_cache(self) -> CacheClearResponse:
        """Clear disposable metadata while preserving covers, genres and catalog."""
        try:
            memory_entries = self._cache.size()

            metadata_stats = await asyncio.to_thread(self._disk_cache.get_stats)
            metadata_count = metadata_stats["total_count"]
            await self._disk_cache.clear_all()
            response_entries = await self._mb_response_store.clear()

            await self._cache.clear()

            return CacheClearResponse(
                success=True,
                message=(
                    f"Successfully cleared {memory_entries} memory entries and "
                    f"{metadata_count} metadata files and {response_entries} response entries (covers preserved)"
                ),
                cleared_memory_entries=memory_entries,
                cleared_disk_files=metadata_count,
                cover_files_cleared=0,
                cleared_response_entries=response_entries,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to clear metadata cache: {e}")
            return CacheClearResponse(
                success=False,
                message=f"Failed to clear metadata cache: {str(e)}",
                cleared_memory_entries=0,
                cleared_disk_files=0,
                cover_files_cleared=0,
            )
        finally:
            self._invalidate_stats()

    async def clear_all_cache(self) -> CacheClearResponse:
        covers_cache_dir = get_covers_cache_dir()
        try:
            memory_entries = self._cache.size()

            metadata_stats = await asyncio.to_thread(self._disk_cache.get_stats)
            metadata_count = metadata_stats["total_count"]
            await self._disk_cache.clear_all()
            response_entries = await self._mb_response_store.clear()
            await self._cache.clear()

            cover_files, _ = await asyncio.to_thread(
                self._scan_covers, covers_cache_dir, delete=True
            )
            genre_files = await asyncio.to_thread(self._clear_genre_disk_cache)

            return CacheClearResponse(
                success=True,
                message=f"Successfully cleared {memory_entries} memory entries, {metadata_count} metadata files, {cover_files} cover files, {genre_files} genre files and {response_entries} response entries (library database preserved)",
                cleared_memory_entries=memory_entries,
                cleared_disk_files=cover_files + genre_files + metadata_count,
                cover_files_cleared=cover_files,
                cleared_response_entries=response_entries,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to clear all cache: {e}")
            return CacheClearResponse(
                success=False,
                message=f"Couldn't clear the cache: {str(e)}",
                cleared_memory_entries=0,
                cleared_disk_files=0,
            )
        finally:
            self._invalidate_stats()

    async def clear_covers_cache(self) -> CacheClearResponse:
        covers_cache_dir = get_covers_cache_dir()
        try:
            files_cleared, _ = await asyncio.to_thread(
                self._scan_covers, covers_cache_dir, delete=True
            )

            return CacheClearResponse(
                success=True,
                message=f"Successfully cleared {files_cleared} cover images",
                cleared_memory_entries=0,
                cleared_disk_files=files_cleared,
                cover_files_cleared=files_cleared,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to clear covers cache: {e}")
            return CacheClearResponse(
                success=False,
                message=f"Failed to clear covers cache: {str(e)}",
                cleared_memory_entries=0,
                cleared_disk_files=0,
            )
        finally:
            self._invalidate_stats()

    async def clear_library_cache(self) -> CacheClearResponse:
        try:
            lib_stats = await self._library_db.get_stats()
            artists_before = lib_stats["artist_count"]
            albums_before = lib_stats["album_count"]

            await self._library_db.clear()


            return CacheClearResponse(
                success=True,
                message=f"Successfully cleared library database: {artists_before} artists, {albums_before} albums",
                cleared_memory_entries=0,
                cleared_disk_files=0,
                cleared_library_artists=artists_before,
                cleared_library_albums=albums_before,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to clear library cache: {e}")
            return CacheClearResponse(
                success=False,
                message=f"Failed to clear library cache: {str(e)}",
                cleared_memory_entries=0,
                cleared_disk_files=0,
            )
        finally:
            self._invalidate_stats()

    async def clear_audiodb(self) -> CacheClearResponse:
        try:
            stats_before = await asyncio.to_thread(self._disk_cache.get_stats)
            count_before = stats_before.get(
                "audiodb_artist_count", 0
            ) + stats_before.get("audiodb_album_count", 0)

            await self._disk_cache.clear_audiodb()
            memory_cleared = await self._cache.clear_prefix(AUDIODB_PREFIX)


            return CacheClearResponse(
                success=True,
                message=f"Successfully cleared {count_before} AudioDB cache entries and {memory_cleared} memory entries",
                cleared_memory_entries=memory_cleared,
                cleared_disk_files=count_before,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to clear AudioDB cache: {e}")
            return CacheClearResponse(
                success=False,
                message=f"Failed to clear AudioDB cache: {str(e)}",
                cleared_memory_entries=0,
                cleared_disk_files=0,
            )
        finally:
            self._invalidate_stats()
