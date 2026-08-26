"""Native target-library read projection with explicit local and provider IDs."""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import Any

from api.v1.schemas.library_target import (
    ActiveEditionConversionSummary,
    TargetNativeAlbum,
    TargetNativeAlbumDetail,
    TargetNativeAlbumStatusResponse,
    TargetNativeArtist,
    TargetNativeArtistAppearance,
    TargetNativeProviderIdsResponse,
    TargetNativeStatsResponse,
    TargetNativeTrack,
)
from api.v1.schemas.library import ResolvedTrack, TrackResolveItem, TrackResolveResponse
from infrastructure.persistence.native_library_store import NativeLibraryStore
from core.exceptions import ResourceNotFoundError
from models.edition_management import CustomEditionState
from services.native.quality_tiers import tier_for, tier_rank
from services.native.library_policy_resolver import LibraryPolicyResolver
from services.native.identification_revisions import album_input_revisions
from models.library_work import ScanScope


class TargetNativeLibraryService:
    def __init__(self, store: NativeLibraryStore) -> None:
        self._store = store

    async def canonical_id(self, kind: str, identifier: str) -> str | None:
        return await self._store.resolve_canonical_target_id(kind, identifier)

    async def albums(
        self,
        *,
        limit: int,
        offset: int,
        sort: str,
        search: str | None,
        file_format: str | None,
    ) -> tuple[list[TargetNativeAlbum], int]:
        rows, total = await self._store.list_target_albums(
            limit=limit,
            offset=offset,
            sort=sort,
            search=search,
            file_format=file_format,
        )
        return [self._album(row) for row in rows], total

    async def artists(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None,
        sort_by: str = "name",
        sort_order: str,
        scope: str = "album",
    ) -> tuple[list[TargetNativeArtist], int]:
        rows, total = await self._store.list_target_artists(
            limit=limit,
            offset=offset,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
            scope=scope,
        )
        return [self._artist(row) for row in rows], total

    async def artist_scope_counts(self) -> tuple[int, int]:
        return await self._store.target_artist_scope_counts()

    async def artist(self, artist_id: str) -> TargetNativeArtist | None:
        canonical = await self.canonical_id("artist", artist_id)
        if canonical is None:
            return None
        rows, _ = await self._store.list_target_artists(
            limit=1, offset=0, artist_ids=[canonical], scope="all"
        )
        return self._artist(rows[0]) if rows else None

    async def artist_albums(self, artist_id: str) -> list[TargetNativeAlbum]:
        canonical = await self.canonical_id("artist", artist_id)
        if canonical is None:
            return []
        rows, _ = await self._store.list_target_albums(
            limit=10_000, offset=0, sort="name", artist_id=canonical
        )
        return [self._album(row) for row in rows]

    async def artist_appearances(
        self, artist_id: str, *, limit: int, offset: int
    ) -> tuple[list[TargetNativeArtistAppearance], int, int]:
        canonical = await self.canonical_id("artist", artist_id)
        if canonical is None:
            return [], 0, 0
        rows, total, total_tracks = await self._store.list_target_artist_appearances(
            canonical, limit=limit, offset=offset
        )
        return (
            [
                TargetNativeArtistAppearance(
                    album=self._album(row["album"]),
                    tracks=[self._track(track) for track in row["tracks"]],
                )
                for row in rows
            ],
            total,
            total_tracks,
        )

    async def tracks(
        self,
        *,
        limit: int,
        offset: int,
        sort: str,
        search: str | None,
    ) -> tuple[list[TargetNativeTrack], int]:
        rows, total = await self._store.list_target_tracks(
            limit=limit, offset=offset, sort=sort, search=search
        )
        return [self._track(row) for row in rows], total

    async def album_tracks(self, album_id: str) -> list[TargetNativeTrack]:
        return [
            self._track(row)
            for row in await self._store.get_target_album_tracks(album_id)
        ]

    async def album(self, album_id: str) -> TargetNativeAlbum | None:
        canonical = await self.canonical_id("album", album_id)
        if canonical is None:
            return None
        rows, _ = await self._store.list_target_albums(
            limit=1, offset=0, sort="name", album_ids=[canonical]
        )
        return self._album(rows[0]) if rows else None

    async def album_copies(self, album_id: str) -> list[TargetNativeAlbum]:
        rows, _ = await self._store.list_target_albums(
            limit=1_000, offset=0, sort="name", album_ids=[album_id]
        )
        return [self._album(row) for row in rows]

    async def album_detail(self, album_id: str) -> TargetNativeAlbumDetail | None:
        album = await self.album(album_id)
        if album is None:
            return None
        context = await self._store.get_album_identification_context(album.id)
        if context is None:
            return None
        identity = context["identity"]
        review = context["review"]
        tracks = [
            track for track in context["tracks"] if track["availability"] == "indexed"
        ]
        contribution, custom, exclusion, conversion = await asyncio.gather(
            self._store.get_active_album_contribution(album.id),
            self._store.get_custom_edition_state(album.id),
            self._store.get_management_exclusion(album.id),
            self._store.get_active_edition_conversion(album.id),
        )
        if (
            identity is not None
            and review is not None
            and review["state"] == "needs_review"
        ):
            status = "manual_identity_needs_review"
        elif identity is not None:
            status = "identified"
        elif review is not None and review["state"] == "keep_tagged":
            status = "keep_tagged"
        elif review is not None and review["state"] == "needs_review":
            status = "needs_review"
        else:
            status = "local_metadata"
        album_values = {
            field: getattr(album, field)
            for field in TargetNativeAlbum.__struct_fields__
            if field not in {"contribution_id", "contribution_state"}
        }
        if custom is not None:
            album_values["album_identity_state"] = "custom_edition"
            album_values["musicbrainz_release_id"] = None
        return TargetNativeAlbumDetail(
            **album_values,
            row_revision=int(context["album"]["row_revision"]),
            input_revision=":".join(album_input_revisions(tracks)),
            identification_status=status,
            review_id=str(review["id"]) if review is not None else None,
            review_revision=int(review["row_revision"]) if review is not None else None,
            management_identity_readiness=self._management_identity_readiness(
                identity, tracks, custom=custom, excluded=exclusion is not None
            ),
            mapped_track_count=(
                custom.recognized_track_count
                if custom is not None
                else self._mapped_track_count(identity, tracks)
            ),
            management_identity_kind=(
                "custom_edition"
                if custom is not None
                else "exact_release"
                if identity is not None and identity["release_mbid"]
                else None
            ),
            custom_manifest_id=(custom.manifest.id if custom is not None else None),
            custom_manifest_version=(
                custom.manifest.version if custom is not None else None
            ),
            custom_manifest_track_count=(
                len(custom.manifest.tracks) if custom is not None else 0
            ),
            custom_manifest_recognized_track_count=(
                custom.recognized_track_count if custom is not None else 0
            ),
            custom_manifest_stale=custom.stale if custom is not None else False,
            management_excluded=exclusion is not None,
            management_exclusion_revision=(
                exclusion.row_revision if exclusion is not None else None
            ),
            management_excluded_at=(
                exclusion.excluded_at if exclusion is not None else None
            ),
            active_edition_conversion=(
                ActiveEditionConversionSummary(
                    job_id=conversion.id,
                    release_mbid=conversion.target_release_mbid,
                    state=conversion.state,
                    kept_count=conversion.kept_count,
                    acquire_count=conversion.acquire_count,
                    staged_count=conversion.staged_count,
                    failed_count=conversion.failed_count,
                    recycle_count=conversion.recycle_count,
                    row_revision=conversion.row_revision,
                    final_preview_job_id=conversion.final_preview_job_id,
                )
                if conversion is not None
                else None
            ),
            contribution_id=(
                str(contribution["id"]) if contribution is not None else None
            ),
            contribution_state=(
                str(contribution["state"]) if contribution is not None else None
            ),
        )

    async def track(self, track_id: str) -> TargetNativeTrack | None:
        row = await self._store.get_target_track(track_id)
        return self._track(row) if row is not None else None

    async def recently_added(self, limit: int) -> list[TargetNativeAlbum]:
        rows, _ = await self._store.list_target_albums(
            limit=limit, offset=0, sort="recent"
        )
        return [self._album(row) for row in rows]

    async def resolve_tracks(
        self, items: list[TrackResolveItem]
    ) -> TrackResolveResponse:
        resolved: list[ResolvedTrack] = []
        album_cache: dict[str, dict[tuple[int, int], TargetNativeTrack]] = {}
        for item in items[:200]:
            album_id = item.release_group_mbid
            if album_id is None or item.track_number is None:
                resolved.append(
                    ResolvedTrack(
                        release_group_mbid=album_id,
                        disc_number=item.disc_number,
                        track_number=item.track_number,
                    )
                )
                continue
            canonical = await self.canonical_id("album", album_id)
            if canonical is None:
                resolved.append(
                    ResolvedTrack(
                        release_group_mbid=album_id,
                        disc_number=item.disc_number,
                        track_number=item.track_number,
                    )
                )
                continue
            if canonical not in album_cache:
                album_cache[canonical] = {
                    (track.disc_number, track.track_number): track
                    for track in await self.album_tracks(canonical)
                }
            match = album_cache[canonical].get(
                (item.disc_number or 1, item.track_number)
            )
            resolved.append(
                ResolvedTrack(
                    release_group_mbid=album_id,
                    disc_number=item.disc_number,
                    track_number=item.track_number,
                    source="local" if match is not None else None,
                    track_source_id=match.id if match is not None else None,
                    stream_url=(
                        f"/api/v1/stream/local/{match.id}"
                        if match is not None
                        else None
                    ),
                    format=match.format if match is not None else None,
                    duration=(match.duration_seconds if match is not None else None),
                )
            )
        return TrackResolveResponse(items=resolved)

    async def album_rescan_scopes(
        self, album_id: str, resolver: LibraryPolicyResolver
    ) -> list[ScanScope]:
        canonical = await self.canonical_id("album", album_id)
        if canonical is None:
            return []
        root_paths = {root.id: root.path for root in resolver.settings.library_roots}
        scopes: dict[tuple[str, str], ScanScope] = {}
        for row in await self._store.get_target_album_tracks(canonical):
            resolved = resolver.resolve(str(row["file_path"]))
            if resolved is None:
                continue
            parent = PurePosixPath(resolved.relative_path).parent.as_posix()
            relative = parent if parent not in ("", ".") else "."
            key = (resolved.root_id, relative)
            scopes[key] = ScanScope(
                root_id=resolved.root_id,
                scope_id=f"album:{canonical}:{relative}",
                relative_path=relative,
                root_path=root_paths[resolved.root_id],
                effective_policy=resolved.policy,
                policy_revision=resolver.policy_revision,
            )
        return list(scopes.values())

    async def album_status(
        self,
        album_id: str,
        *,
        quality_cutoff: str | None,
        upgrade_allowed: bool,
    ) -> TargetNativeAlbumStatusResponse:
        tracks = await self.album_tracks(album_id)
        canonical = (
            tracks[0].album_id
            if tracks
            else (await self.canonical_id("album", album_id) or album_id)
        )
        for track in tracks:
            track.current_tier = tier_for(track.format, track.bit_rate)
            track.below_cutoff = bool(
                upgrade_allowed
                and quality_cutoff
                and tier_rank(track.current_tier) < tier_rank(quality_cutoff)
            )
        return TargetNativeAlbumStatusResponse(
            in_library=bool(tracks),
            album_id=canonical,
            track_count=len(tracks),
            tracks=tracks,
        )

    async def stats(self) -> TargetNativeStatsResponse:
        row = await self._store.get_target_library_stats()
        return TargetNativeStatsResponse(
            total_albums=row["total_albums"],
            total_artists=row["total_artists"],
            total_tracks=row["total_tracks"],
            total_size_bytes=row["total_size_bytes"],
            format_breakdown=row["format_breakdown"],
            review_count=row["unmatched_count"],
            local_only_count=row["local_only_count"],
            last_scan_at=row["last_scan_at"],
        )

    async def provider_ids(self) -> TargetNativeProviderIdsResponse:
        _revision, values = await self._store.target_provider_album_snapshot()
        return TargetNativeProviderIdsResponse(
            musicbrainz_release_group_ids=sorted(values)
        )

    @staticmethod
    def _management_identity_readiness(
        identity: dict[str, Any] | None,
        tracks: list[dict[str, Any]],
        *,
        custom: CustomEditionState | None = None,
        excluded: bool = False,
    ) -> str:
        if not tracks or excluded:
            return "not_applicable"
        if custom is not None:
            return "custom_manifest_stale" if custom.stale else "ready"
        if (
            identity is None
            or not identity["release_group_mbid"]
            or not identity["release_mbid"]
        ):
            return "exact_release_required"
        release_track_ids: set[str] = set()
        for track in tracks:
            release_track_mbid = track["release_track_mbid"]
            if (
                not track["identity_row_revision"]
                or not track["recording_mbid"]
                or not release_track_mbid
                or track["identity_release_mbid"] != identity["release_mbid"]
                or track["medium_position"] is None
                or track["release_track_position"] is None
                or release_track_mbid in release_track_ids
            ):
                return "track_mapping_required"
            release_track_ids.add(str(release_track_mbid))
        return "ready"

    async def reenable_album_management(
        self,
        album_id: str,
        *,
        expected_exclusion_revision: int,
        actor_user_id: str,
        now: float,
    ) -> bool:
        canonical = await self.canonical_id("album", album_id)
        if canonical is None:
            raise ResourceNotFoundError("Library album not found.")
        return await self._store.clear_management_exclusion(
            canonical,
            expected_row_revision=expected_exclusion_revision,
            actor_user_id=actor_user_id,
            now=now,
        )

    @staticmethod
    def _mapped_track_count(
        identity: dict[str, Any] | None, tracks: list[dict[str, Any]]
    ) -> int:
        if identity is None or not identity["release_mbid"]:
            return 0
        release_track_ids = {
            str(track["release_track_mbid"])
            for track in tracks
            if track["identity_row_revision"]
            and track["recording_mbid"]
            and track["release_track_mbid"]
            and track["identity_release_mbid"] == identity["release_mbid"]
            and track["medium_position"] is not None
            and track["release_track_position"] is not None
        }
        return len(release_track_ids)

    @staticmethod
    def _album(row: dict[str, Any]) -> TargetNativeAlbum:
        release_group_mbid = row.get("provider_release_group_mbid")
        release_mbid = row.get("provider_release_mbid")
        if row.get("custom_manifest_id"):
            identity_state = "custom_edition"
            release_mbid = None
        elif release_mbid:
            identity_state = "release_linked"
        elif release_group_mbid:
            identity_state = "release_group_linked"
        else:
            identity_state = "local_only"
        return TargetNativeAlbum(
            id=str(row["release_group_mbid"]),
            title=str(row["album_title"]),
            artist_name=str(row.get("album_artist_name") or ""),
            artist_id=str(row.get("album_artist_mbid") or ""),
            musicbrainz_release_group_id=release_group_mbid,
            musicbrainz_release_id=release_mbid,
            musicbrainz_artist_id=row.get("provider_artist_mbid"),
            album_identity_state=identity_state,
            track_count=int(row.get("track_count") or 0),
            total_duration_seconds=float(row.get("total_duration_seconds") or 0),
            total_size_bytes=int(row.get("total_size_bytes") or 0),
            format=row.get("file_format"),
            year=row.get("year"),
            is_compilation=bool(row.get("is_compilation")),
            cover_available=bool(
                row.get("cover_url")
                or row.get("artwork_source")
                or row.get("provider_release_group_mbid")
            ),
            date_added=row.get("last_imported_at"),
            sort_name=row.get("album_sort_name"),
            original_release_date=row.get("original_release_date"),
            contribution_id=row.get("contribution_id"),
            contribution_state=row.get("contribution_state"),
        )

    @staticmethod
    def _artist(row: dict[str, Any]) -> TargetNativeArtist:
        provider_artist_id = row.get("provider_artist_mbid")
        return TargetNativeArtist(
            id=str(row["artist_mbid"]),
            name=str(row["artist_name"]),
            musicbrainz_artist_id=provider_artist_id,
            artist_identity_state=(
                "musicbrainz_linked" if provider_artist_id else "local_only"
            ),
            album_count=int(row.get("album_count") or 0),
            track_count=int(row.get("track_count") or 0),
            appearance_release_count=int(row.get("appearance_release_count") or 0),
            appearance_track_count=int(row.get("appearance_track_count") or 0),
            library_relationship=str(row.get("library_relationship") or "album_artist"),
            date_added=row.get("date_added"),
            row_revision=int(row.get("row_revision") or 1),
        )

    @staticmethod
    def _track(row: dict[str, Any]) -> TargetNativeTrack:
        return TargetNativeTrack(
            id=str(row["id"]),
            title=str(row.get("track_title") or ""),
            album_id=str(row.get("release_group_mbid") or ""),
            album_title=str(
                row.get("canonical_album_title") or row.get("album_title") or ""
            ),
            artist_id=str(row.get("artist_mbid") or ""),
            artist_name=str(row.get("artist_name") or ""),
            album_artist_id=str(row.get("album_artist_mbid") or ""),
            album_artist_name=str(row.get("album_artist_name") or ""),
            musicbrainz_recording_id=row.get("recording_mbid"),
            musicbrainz_release_group_id=row.get("provider_release_group_mbid"),
            musicbrainz_artist_id=row.get("provider_artist_mbid"),
            musicbrainz_album_artist_id=row.get("provider_album_artist_mbid"),
            disc_number=int(row.get("disc_number") or 1),
            track_number=int(row.get("track_number") or 0),
            year=row.get("year"),
            genre=row.get("genre"),
            duration_seconds=float(row.get("duration_seconds") or 0),
            format=str(row.get("file_format") or ""),
            bit_rate=row.get("bit_rate"),
            sample_rate=row.get("sample_rate"),
            bit_depth=row.get("bit_depth"),
            channels=row.get("channels"),
            file_size_bytes=int(row.get("file_size_bytes") or 0),
            date_added=row.get("imported_at"),
            cover_available=bool(
                row.get("cover_url")
                or row.get("artwork_source")
                or row.get("provider_release_group_mbid")
            ),
        )
