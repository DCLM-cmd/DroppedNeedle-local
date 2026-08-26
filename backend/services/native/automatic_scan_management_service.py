"""Schedule opted-in management after scan discovery gains a full identity."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from api.v1.schemas.library_management import (
    LibraryManagementProfile,
    LibraryManagementSettings,
    profile_revision,
    settings_revision,
)
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_management import LibraryManagementJobSnapshot
from models.library_management_canonical import AcceptedAlbumManagementIdentity
from models.library_management_planning import (
    LibraryManagementSelection,
    naming_policy_revision,
)
from services.native.identification_revisions import album_input_revisions
from services.native.library_management_planner import LibraryManagementPlanner
from services.native.library_management_profile_service import (
    LibraryManagementProfileService,
)
from services.native.library_policy_resolver import LibraryPolicyResolver


class AutomaticScanManagementService:
    def __init__(
        self,
        store: NativeLibraryStore,
        profiles: LibraryManagementProfileService,
        planner: LibraryManagementPlanner,
    ) -> None:
        self._store = store
        self._profiles = profiles
        self._planner = planner

    async def schedule_scanned_album(self, local_album_id: str) -> str | None:
        context = await self._store.get_album_identification_context(local_album_id)
        if context is None:
            return None
        tracks = [
            track for track in context["tracks"] if track["availability"] == "indexed"
        ]
        if not tracks:
            return None
        return await self._schedule_identified_context(
            local_album_id,
            album_input_revisions(tracks)[2],
            context,
        )

    async def schedule_identified_album(
        self,
        local_album_id: str,
        expected_input_policy_revision: str,
    ) -> str | None:
        """Queue one deterministic album operation, or wait for complete mappings."""

        context = await self._store.get_album_identification_context(local_album_id)
        if context is None or not context["tracks"]:
            return None
        return await self._schedule_identified_context(
            local_album_id, expected_input_policy_revision, context
        )

    async def _schedule_identified_context(
        self,
        local_album_id: str,
        expected_input_policy_revision: str,
        context: dict,
    ) -> str | None:
        tracks = [
            track for track in context["tracks"] if track["availability"] == "indexed"
        ]
        if not tracks:
            return None
        if album_input_revisions(tracks)[2] != expected_input_policy_revision:
            return None
        tag_revision, file_revision, input_policy_revision = album_input_revisions(
            tracks
        )
        applied_policy_revisions = {
            str(track["applied_policy_revision"]) for track in tracks
        }
        if len(applied_policy_revisions) != 1:
            return None
        policy_revision = next(iter(applied_policy_revisions))
        root_ids = {str(track["root_id"]) for track in tracks}
        if len(root_ids) != 1:
            return None
        root_id = next(iter(root_ids))
        resolved = self._profiles.prepare_automatic_profile(
            root_id=root_id,
            trigger="scan_discovered",
            expected_policy_revision=policy_revision,
        )
        if resolved is None:
            return None
        settings, assignment, profile, policy = resolved
        if await self._restored_after_activation(
            tracks, assignment.activation_confirmed_at
        ):
            return None
        track_ids = tuple(str(track["id"]) for track in tracks)
        identity = await self._store.get_accepted_library_management_identity(
            local_album_id,
            local_track_ids=track_ids,
        )
        if await self._store.get_management_exclusion(local_album_id) is not None:
            return None
        if identity is not None and identity.identity_kind == "custom_edition":
            identity_ready = (
                assignment.automatic_custom_editions
                and not identity.custom_manifest_stale
                and bool(identity.custom_manifest_id)
                and identity.identity_revision is not None
                and bool(identity.release_group_mbid)
                and len(identity.tracks) == len(track_ids)
                and all(
                    track.recording_mbid is None or track.identity_revision is not None
                    for track in identity.tracks
                )
            )
        else:
            identity_ready = not (
                identity is None
                or identity.identity_revision is None
                or not identity.release_group_mbid
                or not identity.release_mbid
                or len(identity.tracks) != len(track_ids)
                or any(
                    track.identity_revision is None
                    or not track.recording_mbid
                    or not track.release_track_mbid
                    or track.medium_position is None
                    or track.release_track_position is None
                    or track.release_mbid != identity.release_mbid
                    for track in identity.tracks
                )
            )
        if not identity_ready:
            return None
        assert identity is not None
        identity_revision = ":".join(
            [
                str(identity.identity_revision),
                identity.identity_kind,
                identity.custom_manifest_id or "",
                *(
                    f"{track.local_track_id}:{track.identity_revision}:"
                    f"{track.release_track_mbid}"
                    for track in identity.tracks
                ),
            ]
        )
        if await self._matches_committed_management(
            tracks=tracks,
            identity=identity,
            settings=settings,
            profile=profile,
            policy=policy,
            policy_revision=policy_revision,
        ):
            return None
        idempotency_material = "\x00".join(
            (
                local_album_id,
                identity_revision,
                tag_revision,
                file_revision,
                policy_revision,
                profile_revision(profile),
                settings_revision(settings),
            )
        )
        idempotency_key = (
            "automatic-scan:"
            + hashlib.sha256(idempotency_material.encode("utf-8")).hexdigest()
        )
        handle = await self._planner.create_preview(
            selection=LibraryManagementSelection(kind="albums", ids=(local_album_id,)),
            profile_id=profile.id,
            expected_settings_revision=settings_revision(settings),
            expected_policy_revision=policy_revision,
            actor_user_id=None,
            idempotency_key=idempotency_key,
            origin="scan_discovered",
            settings_snapshot=settings,
            effective_profile=profile,
        )
        return handle.job_id

    async def _restored_after_activation(
        self, tracks: list[dict], activation_confirmed_at: float | None
    ) -> bool:
        if activation_confirmed_at is None:
            return True
        for track in tracks:
            state = await self._store.get_track_management_state(str(track["id"]))
            if (
                state is not None
                and state.last_outcome == "restored"
                and (
                    state.last_managed_at is None
                    or state.last_managed_at >= activation_confirmed_at
                )
            ):
                return True
        return False

    async def _matches_committed_management(
        self,
        *,
        tracks: list[dict],
        identity: AcceptedAlbumManagementIdentity,
        settings: LibraryManagementSettings,
        profile: LibraryManagementProfile,
        policy: LibraryPolicyResolver,
        policy_revision: str,
    ) -> bool:
        profile_hash = profile_revision(profile)
        pinned = self._planner.pin_profile(settings, profile)
        current_naming_revision = naming_policy_revision(pinned)
        roots = {root.id: root for root in policy.settings.library_roots}
        identity_tracks = {value.local_track_id: value for value in identity.tracks}
        operation_cache: dict[
            str, tuple[dict | None, LibraryManagementJobSnapshot | None]
        ] = {}
        for track in tracks:
            track_id = str(track["id"])
            state = await self._store.get_track_management_state(track_id)
            if (
                state is None
                or state.last_outcome != "succeeded"
                or not state.last_operation_job_id
                or state.applied_profile_id != profile.id
                or state.applied_profile_revision != profile_hash
                or not state.applied_projection_hash
                or state.applied_naming_script_revision != current_naming_revision
                or state.managed_root_id != str(track["root_id"])
            ):
                return False
            override_revision = await self._store.get_management_override_revision(
                str(track["local_album_id"]), track_id
            )
            if state.applied_override_revision != override_revision:
                return False
            job_id = state.last_operation_job_id
            cached = operation_cache.get(job_id)
            if cached is None:
                cached = (
                    await self._store.get_operation_job(job_id),
                    await self._store.get_library_management_job_snapshot(job_id),
                )
                operation_cache[job_id] = cached
            operation, snapshot = cached
            if (
                operation is None
                or operation["state"] != "succeeded"
                or snapshot is None
                or snapshot.policy_revision != policy_revision
                or snapshot.settings_revision != settings_revision(settings)
            ):
                return False
            item = await self._store.get_library_management_plan_item_for_track(
                job_id, track_id
            )
            mapped = identity_tracks.get(track_id)
            if (
                item is None
                or mapped is None
                or item.expected_album_identity_revision != identity.identity_revision
                or item.expected_identity_revision != mapped.identity_revision
            ):
                return False
            root = roots.get(str(track["root_id"]))
            if root is None:
                return False
            path_revision = await asyncio.to_thread(
                self._managed_path_revision, track, Path(root.path)
            )
            if path_revision != state.managed_path_revision:
                return False
        return True

    @classmethod
    def _managed_path_revision(cls, track: dict, root: Path) -> str | None:
        relative = PurePosixPath(str(track["relative_path"]))
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            return None
        path = root.joinpath(*relative.parts)
        if Path(os.path.normpath(str(path))) != Path(
            os.path.normpath(str(track["file_path"]))
        ):
            return None
        try:
            fingerprint = cls._hash_file(path)
        except OSError:
            return None
        return hashlib.sha256(
            f"{track['root_id']}\x00{relative.as_posix()}\x00{fingerprint}".encode()
        ).hexdigest()

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as input_file:
            if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
                raise OSError("Managed path is not a regular file.")
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
