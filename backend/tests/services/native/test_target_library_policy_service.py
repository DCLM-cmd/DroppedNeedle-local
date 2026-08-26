from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call

import msgspec
import pytest

from api.v1.schemas.library_policies import (
    LibraryPolicyApplyRequest,
    LibraryRestorableRoot,
    LibraryRestorableRootsResponse,
    LibraryRestoreRootsRequest,
    LibraryRootSettings,
    LibrarySettingsResponse,
    TypedLibrarySettings,
)
from core.exceptions import ValidationError
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_work import ScanRequest, ScanScope
from services.native.library_policy_service import LibraryPolicyService
from services.native.library_policy_reconciliation_service import (
    LibraryPolicyReconciliationService,
)
from services.native.target_library_policy_service import TargetLibraryPolicyService
from services.native.library_policy_resolver import LibraryPolicyResolver


@pytest.mark.asyncio
async def test_policy_pending_state_survives_refresh_and_clears_only_after_apply(
    tmp_path: Path,
) -> None:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users(id) VALUES ('admin')")
    connection.close()
    store = NativeLibraryStore(path, threading.Lock())
    settings = TypedLibrarySettings(
        library_roots=[
            LibraryRootSettings(
                id="root", path="/music", label="Music", policy="excluded"
            )
        ]
    )
    previous_settings = TypedLibrarySettings(
        library_roots=[
            LibraryRootSettings(
                id="root", path="/music", label="Music", policy="automatic"
            )
        ]
    )
    current = LibrarySettingsResponse(
        library_roots=settings.library_roots,
        policy_revision="policy-2",
    )
    saved = LibrarySettingsResponse(
        library_roots=settings.library_roots,
        policy_revision="policy-2",
        reconciliation_required=True,
        reconciliation_state="awaiting_reconciliation",
        pending_policy_revision="policy-2",
        affected_scope_ids=["root"],
    )
    base = Mock()
    base.get_settings.return_value = current
    base.current_settings.return_value = previous_settings
    base.current_settings_raw.return_value = previous_settings
    scope = ScanScope(
        root_id="root",
        scope_id="root",
        root_path="/music",
        effective_policy="excluded",
        policy_revision="policy-2",
    )
    proposed = Mock(policy_revision="policy-2", settings=settings)
    base.prepare_change.return_value = (proposed, [scope])
    base.rebase_scopes.return_value = []
    base.collapse_scopes.side_effect = lambda scopes: scopes
    reconciliation = AsyncMock()

    async def commit_boundary(*, proposed_policy_revision: str) -> dict[str, int]:
        await store.record_pending_policy(
            policy_revision=proposed_policy_revision,
            scopes=[scope],
            changed_track_count=0,
            cancelled_work_count=1,
            updated_at=2,
        )
        return {"changed": 0, "cancelled": 1}

    reconciliation.commit_boundary.side_effect = commit_boundary
    reconciliation.preview_apply.return_value = {
        "policy_revision": "policy-2",
        "scope_ids": ["root"],
        "estimated_file_count": 12,
        "scopes": [scope],
    }
    on_settings_saved = Mock()
    service = TargetLibraryPolicyService(
        base,
        reconciliation,
        store,
        on_settings_saved=on_settings_saved,
    )

    response = await service.save_settings(
        settings, expected_policy_revision="policy-1"
    )
    refreshed = await service.get_settings()
    preview = await service.preview_apply(
        LibraryPolicyApplyRequest(
            scope_ids=["root"], expected_policy_revision="policy-2"
        )
    )
    assert response.reconciliation_state == "awaiting_reconciliation"
    assert response.affected_scope_ids == ["root"]
    assert response.actions_applied == [
        "Settings saved. No library work was started.",
        "1 queued identification job was stopped because the new policy no longer allows the work.",
    ]
    assert refreshed.reconciliation_required is True
    assert refreshed.pending_policy_revision == "policy-2"
    assert preview.estimated_file_count == 12
    assert preview.content_will_become_unavailable is True
    assert preview.queued_work_was_cancelled_on_save is True
    on_settings_saved.assert_called_once_with()

    requested = await store.request_scan_run(
        ScanRequest(
            kind="policy_reconcile",
            trigger="policy_apply",
            scopes=[
                ScanScope(
                    root_id="root",
                    scope_id="root",
                    root_path="/music",
                    effective_policy="excluded",
                    policy_revision="policy-2",
                )
            ],
            policy_revision="policy-2",
            requested_by_user_id="admin",
        ),
        run_id="policy-apply",
        requested_at=3,
    )
    run = await store.transition_scan_run(
        requested.run_id,
        expected_state="queued",
        expected_revision=requested.row_revision,
        new_state="discovering",
        now=4,
    )
    for expected, target in (
        ("discovering", "indexing"),
        ("indexing", "reconciling"),
        ("reconciling", "completed"),
    ):
        run = await store.transition_scan_run(
            run.id,
            expected_state=expected,
            expected_revision=run.row_revision,
            new_state=target,
            now=run.updated_at + 1,
        )
    applied = await service.get_settings()
    assert applied.reconciliation_required is False
    assert applied.reconciliation_state == "applied"
    assert applied.pending_policy_revision is None


@pytest.mark.asyncio
async def test_policy_boundary_failure_restores_config_and_aborts_journal() -> None:
    previous = TypedLibrarySettings(
        library_roots=[
            LibraryRootSettings(
                id="root", path="/music", label="Music", policy="automatic"
            )
        ]
    )
    proposed_settings = msgspec.structs.replace(
        previous,
        library_roots=[
            msgspec.structs.replace(previous.library_roots[0], policy="excluded")
        ],
    )
    proposed = LibraryPolicyResolver(proposed_settings)
    base = Mock()
    base.current_settings.return_value = previous
    base.current_settings_raw.return_value = previous
    base.prepare_change.return_value = (proposed, [])
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    reconciliation = AsyncMock()
    reconciliation.commit_boundary.side_effect = RuntimeError("database unavailable")
    store = AsyncMock()
    store.get_pending_policy.return_value = None
    on_settings_saved = Mock()
    service = TargetLibraryPolicyService(
        base,
        reconciliation,
        store,
        on_settings_saved=on_settings_saved,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.save_settings(
            proposed_settings,
            expected_policy_revision=LibraryPolicyResolver(previous).policy_revision,
        )

    assert base.persist_settings.call_args_list == [
        call(
            proposed.settings,
            expected_policy_revision=LibraryPolicyResolver(previous).policy_revision,
        ),
        call(
            previous,
            expected_policy_revision=proposed.policy_revision,
        ),
    ]
    assert on_settings_saved.call_count == 2
    reconciliation.abort_boundary.assert_awaited_once_with(
        proposed_policy_revision=proposed.policy_revision
    )


@pytest.mark.asyncio
async def test_startup_finishes_a_config_committed_policy_transition() -> None:
    settings = TypedLibrarySettings(
        library_roots=[
            LibraryRootSettings(
                id="root", path="/music", label="Music", policy="excluded"
            )
        ]
    )
    revision = LibraryPolicyResolver(settings).policy_revision
    base = Mock()
    base.current_settings.return_value = settings
    reconciliation = AsyncMock()
    store = AsyncMock()
    store.get_policy_transition.return_value = {
        "state": "prepared",
        "previous_policy_revision": "previous",
        "proposed_policy_revision": revision,
    }
    on_settings_saved = Mock()
    service = TargetLibraryPolicyService(
        base,
        reconciliation,
        store,
        on_settings_saved=on_settings_saved,
    )

    assert await service.recover_pending_transition() is True
    on_settings_saved.assert_called_once_with()
    reconciliation.commit_boundary.assert_awaited_once_with(
        proposed_policy_revision=revision
    )
    reconciliation.abort_boundary.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_policy_saves_commit_in_revision_order() -> None:
    root = LibraryRootSettings(
        id="root", path="/music", label="Music", policy="automatic"
    )
    versions = [
        TypedLibrarySettings(library_roots=[root]),
        TypedLibrarySettings(
            library_roots=[msgspec.structs.replace(root, policy="excluded")]
        ),
        TypedLibrarySettings(
            library_roots=[msgspec.structs.replace(root, policy="local_metadata")]
        ),
    ]
    revisions = [LibraryPolicyResolver(value).policy_revision for value in versions]
    state = {"settings": versions[0]}
    base = Mock()
    base.current_settings.side_effect = lambda: state["settings"]
    base.current_settings_raw.side_effect = lambda: state["settings"]

    def prepare(settings, *, expected_policy_revision):
        assert (
            LibraryPolicyResolver(state["settings"]).policy_revision
            == expected_policy_revision
        )
        return LibraryPolicyResolver(settings), []

    def persist(settings, *, expected_policy_revision):
        assert (
            LibraryPolicyResolver(state["settings"]).policy_revision
            == expected_policy_revision
        )
        state["settings"] = settings

    def response():
        resolver = LibraryPolicyResolver(state["settings"])
        return LibrarySettingsResponse(
            library_roots=resolver.settings.library_roots,
            policy_revision=resolver.policy_revision,
        )

    base.prepare_change.side_effect = prepare
    base.persist_settings.side_effect = persist
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    base.get_settings.side_effect = response
    store = AsyncMock()
    store.get_pending_policy.return_value = None
    first_commit_started = asyncio.Event()
    allow_first_commit = asyncio.Event()
    commits: list[str] = []
    reconciliation = AsyncMock()

    async def commit(*, proposed_policy_revision):
        commits.append(proposed_policy_revision)
        if proposed_policy_revision == revisions[1]:
            first_commit_started.set()
            await allow_first_commit.wait()
        return {"changed": 0, "cancelled": 0}

    reconciliation.commit_boundary.side_effect = commit
    service = TargetLibraryPolicyService(base, reconciliation, store)

    first = asyncio.create_task(
        service.save_settings(versions[1], expected_policy_revision=revisions[0])
    )
    await first_commit_started.wait()
    second = asyncio.create_task(
        service.save_settings(versions[2], expected_policy_revision=revisions[1])
    )
    await asyncio.sleep(0)
    assert commits == [revisions[1]]
    allow_first_commit.set()
    await asyncio.gather(first, second)

    assert commits == [revisions[1], revisions[2]]
    assert state["settings"] == versions[2]


@pytest.mark.asyncio
async def test_cancelled_prepare_is_awaited_and_aborted_before_unlocking() -> None:
    previous = TypedLibrarySettings(
        library_roots=[
            LibraryRootSettings(
                id="root", path="/music", label="Music", policy="automatic"
            )
        ]
    )
    proposed_settings = TypedLibrarySettings(
        library_roots=[
            LibraryRootSettings(
                id="root", path="/music", label="Music", policy="excluded"
            )
        ]
    )
    proposed = LibraryPolicyResolver(proposed_settings)
    base = Mock()
    base.current_settings.return_value = previous
    base.current_settings_raw.return_value = previous
    base.prepare_change.return_value = (proposed, [])
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    store = AsyncMock()
    store.get_pending_policy.return_value = None
    prepare_started = asyncio.Event()
    allow_prepare = asyncio.Event()
    reconciliation = AsyncMock()

    async def prepare(**_kwargs):
        prepare_started.set()
        await allow_prepare.wait()

    reconciliation.prepare_boundary.side_effect = prepare
    service = TargetLibraryPolicyService(base, reconciliation, store)
    save = asyncio.create_task(
        service.save_settings(
            proposed_settings,
            expected_policy_revision=LibraryPolicyResolver(previous).policy_revision,
        )
    )
    await prepare_started.wait()
    save.cancel()
    await asyncio.sleep(0)
    assert not save.done()
    allow_prepare.set()

    with pytest.raises(asyncio.CancelledError):
        await save
    reconciliation.abort_boundary.assert_awaited_once_with(
        proposed_policy_revision=proposed.policy_revision
    )
    base.persist_settings.assert_not_called()


def _root(identifier: str, path: str, label: str) -> LibraryRootSettings:
    return LibraryRootSettings(
        id=identifier, path=path, label=label, policy="automatic"
    )


@pytest.mark.asyncio
async def test_empty_roots_save_is_blocked_while_catalog_has_tracks() -> None:
    previous = TypedLibrarySettings(
        library_roots=[_root("root", "/music", "Music")]
    )
    empty = TypedLibrarySettings(library_roots=[])
    proposed = LibraryPolicyResolver(empty)
    base = Mock()
    base.current_settings.return_value = previous
    base.current_settings_raw.return_value = previous
    base.prepare_change.return_value = (proposed, [])
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    store = AsyncMock()
    store.get_pending_policy.return_value = None
    store.catalog_has_tracks.return_value = True
    reconciliation = AsyncMock()
    service = TargetLibraryPolicyService(base, reconciliation, store)

    with pytest.raises(ValidationError, match="orphan the existing catalog"):
        await service.save_settings(
            empty,
            expected_policy_revision=LibraryPolicyResolver(previous).policy_revision,
        )
    store.catalog_has_tracks.assert_awaited_once()
    reconciliation.prepare_boundary.assert_not_awaited()
    base.persist_settings.assert_not_called()


@pytest.mark.asyncio
async def test_empty_roots_save_allowed_when_catalog_is_empty() -> None:
    previous = TypedLibrarySettings(
        library_roots=[_root("root", "/music", "Music")]
    )
    empty = TypedLibrarySettings(library_roots=[])
    proposed = LibraryPolicyResolver(empty)
    revision = LibraryPolicyResolver(previous).policy_revision
    base = Mock()
    base.current_settings.return_value = previous
    base.current_settings_raw.return_value = previous
    base.prepare_change.return_value = (proposed, [])
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    base.get_settings.return_value = LibrarySettingsResponse(
        library_roots=[], policy_revision=proposed.policy_revision
    )
    store = AsyncMock()
    store.get_pending_policy.return_value = None
    store.catalog_has_tracks.return_value = False
    reconciliation = AsyncMock()
    reconciliation.commit_boundary.return_value = {"changed": 0, "cancelled": 0}
    service = TargetLibraryPolicyService(base, reconciliation, store)

    response = await service.save_settings(
        empty, expected_policy_revision=revision
    )
    assert response.library_roots == []
    base.persist_settings.assert_called_once_with(
        proposed.settings, expected_policy_revision=revision
    )
    reconciliation.commit_boundary.assert_awaited_once_with(
        proposed_policy_revision=proposed.policy_revision
    )


@pytest.mark.asyncio
async def test_disabled_save_with_roots_succeeds() -> None:
    previous = TypedLibrarySettings(
        library_roots=[_root("root", "/music", "Music")]
    )
    disabled = TypedLibrarySettings(
        library_roots=[_root("root", "/music", "Music")], enabled=False
    )
    proposed = LibraryPolicyResolver(disabled)
    revision = LibraryPolicyResolver(previous).policy_revision
    base = Mock()
    base.current_settings.return_value = previous
    base.current_settings_raw.return_value = previous
    base.prepare_change.return_value = (proposed, [])
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    base.get_settings.return_value = LibrarySettingsResponse(
        library_roots=disabled.library_roots,
        policy_revision=proposed.policy_revision,
        enabled=False,
    )
    store = AsyncMock()
    store.get_pending_policy.return_value = None
    reconciliation = AsyncMock()
    reconciliation.commit_boundary.return_value = {"changed": 0, "cancelled": 0}
    service = TargetLibraryPolicyService(base, reconciliation, store)

    response = await service.save_settings(
        disabled, expected_policy_revision=revision
    )
    assert response.enabled is False
    assert response.library_roots[0]["id"] == "root"
    assert proposed.settings.enabled is False
    base.persist_settings.assert_called_once_with(
        proposed.settings, expected_policy_revision=revision
    )
    reconciliation.commit_boundary.assert_awaited_once_with(
        proposed_policy_revision=proposed.policy_revision
    )


@pytest.mark.asyncio
async def test_restorable_roots_excludes_configured_and_reports_derived_paths() -> None:
    current = TypedLibrarySettings(
        library_roots=[_root("kept", "/library", "library")]
    )
    base = Mock()
    base.current_settings.return_value = current
    store = AsyncMock()
    store.get_migrated_root_ids.return_value = {"removed", "kept"}
    store.get_restorable_root_paths.return_value = {
        "removed": {"path": "/music", "indexed_file_count": 7}
    }
    service = TargetLibraryPolicyService(base, AsyncMock(), store)

    response = await service.restorable_roots()
    assert response.policy_revision == LibraryPolicyResolver(current).policy_revision
    assert response.restorable_roots == [
        LibraryRestorableRoot(root_id="removed", path="/music", indexed_file_count=7)
    ]


@pytest.mark.asyncio
async def test_restore_roots_reuses_migrated_ids_and_applies_overrides() -> None:
    current = TypedLibrarySettings(
        library_roots=[_root("kept", "/library", "music")]
    )
    base = Mock()
    base.current_settings.return_value = current
    base.current_settings_raw.return_value = current
    base.prepare_change.side_effect = lambda settings, *, expected_policy_revision: (
        LibraryPolicyResolver(settings),
        [],
    )
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    base.get_settings.return_value = LibrarySettingsResponse(
        library_roots=current.library_roots, policy_revision="saved"
    )
    store = AsyncMock()
    store.get_migrated_root_ids.return_value = {"removed-a", "removed-b"}
    store.get_restorable_root_paths.return_value = {
        "removed-a": {"path": "/music", "indexed_file_count": 1},
        "removed-b": {"path": "/vinyl", "indexed_file_count": 2},
    }
    store.get_pending_policy.return_value = None
    reconciliation = AsyncMock()
    reconciliation.commit_boundary.return_value = {"changed": 0, "cancelled": 0}
    service = TargetLibraryPolicyService(base, reconciliation, store)

    await service.restore_roots(
        LibraryRestoreRootsRequest(
            expected_policy_revision="revision-1",
            paths={"removed-a": "/custom"},
        )
    )

    sent = base.persist_settings.call_args.args[0]
    assert {root.id: root.path for root in sent.library_roots} == {
        "kept": "/library",
        "removed-a": "/custom",
        "removed-b": "/vinyl",
    }
    assert {root.id: root.label for root in sent.library_roots} == {
        "kept": "music",
        "removed-a": "custom",
        "removed-b": "vinyl",
    }
    base.persist_settings.assert_called_once_with(
        sent, expected_policy_revision="revision-1"
    )


@pytest.mark.asyncio
async def test_restore_roots_rejects_when_nothing_is_restorable() -> None:
    current = TypedLibrarySettings(
        library_roots=[_root("root", "/music", "Music")]
    )
    base = Mock()
    base.current_settings.return_value = current
    store = AsyncMock()
    store.get_migrated_root_ids.return_value = {"root"}
    service = TargetLibraryPolicyService(base, AsyncMock(), store)

    with pytest.raises(ValidationError, match="no removed library roots"):
        await service.restore_roots(
            LibraryRestoreRootsRequest(expected_policy_revision="revision-1")
        )

    store.get_migrated_root_ids.return_value = {"gone"}
    store.get_restorable_root_paths.return_value = {}
    with pytest.raises(ValidationError, match="no catalog files"):
        await service.restore_roots(
            LibraryRestoreRootsRequest(expected_policy_revision="revision-1")
        )


class _RestorePrefs:
    def __init__(self, roots: list[LibraryRootSettings]) -> None:
        self.roots = list(roots)

    def get_typed_library_settings(self) -> TypedLibrarySettings:
        return TypedLibrarySettings(library_roots=list(self.roots))

    def get_typed_library_settings_raw(self) -> TypedLibrarySettings:
        return self.get_typed_library_settings()

    def save_typed_library_settings_if_current(
        self,
        settings: TypedLibrarySettings,
        *,
        expected_policy_revision: str,
    ) -> None:
        from core.exceptions import StaleRevisionError

        current = LibraryPolicyResolver(
            self.get_typed_library_settings()
        ).policy_revision
        if current != expected_policy_revision:
            raise StaleRevisionError("stale")
        normalized = LibraryPolicyResolver(settings).settings
        self.roots = list(normalized.library_roots)


@pytest.mark.asyncio
async def test_restore_roots_repairs_an_emptied_config_without_doubling(
    tmp_path: Path,
) -> None:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users(id) VALUES ('admin')")
    connection.close()
    store = NativeLibraryStore(path, threading.Lock())
    root_id = "migrated-root"

    def seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO library_migration_runs (id, source_revision, root_revision, "
            "state, report_json, started_at, updated_at, completed_at) "
            "VALUES ('m1','src','p','completed','{}',1,1,1)"
        )
        connection.execute(
            "INSERT INTO library_migration_provenance (source_kind, source_key, "
            "target_kind, target_id, source_revision, imported_at, migration_run_id) "
            "VALUES ('root',?,'library_root',?,'x',1,'m1')",
            (root_id, root_id),
        )
        connection.execute(
            "INSERT INTO local_artists (id, display_name, folded_name, "
            "normalized_name, kind, created_at, updated_at) "
            "VALUES ('artist','Artist','artist','artist','person',1,1)"
        )
        connection.execute(
            "INSERT INTO local_albums (id, root_id, grouping_key, title, title_folded, "
            "album_artist_name, album_artist_name_folded, album_artist_id, "
            "grouping_source, created_at, updated_at) "
            "VALUES ('al1',?,'g','Album','album','Artist','artist','artist',"
            "'automatic',1,1)",
            (root_id,),
        )
        for track_id, relative in (
            ("t1", "Artist One/Album/01. Song.flac"),
            ("t2", "Artist Two/Album/01. Song.flac"),
        ):
            connection.execute(
                "INSERT INTO local_tracks (id, local_album_id, root_id, file_path, "
                "relative_path, path_hash, file_size_bytes, file_mtime_ns, "
                "stat_revision, title, title_folded, artist_name, artist_name_folded, "
                "album_title, album_title_folded, album_artist_name, "
                "album_artist_name_folded, file_format, ingest_source, imported_at, "
                "membership_source) "
                "VALUES (?, 'al1', ?, ?, ?, 'h', 100, 1, 's', 'Song', 'song', "
                "'Artist', 'artist', 'Album', 'album', 'Artist', 'artist', 'flac', "
                "'scan', 1, 'automatic')",
                (track_id, root_id, f"/music/{relative}", relative),
            )
        scope = ScanScope(
            root_id=root_id,
            scope_id=root_id,
            relative_path=".",
            root_path="/music",
            effective_policy="excluded",
            policy_revision="wiped",
        )
        connection.execute(
            "INSERT INTO library_policy_state (singleton, desired_policy_revision, "
            "pending_scope_ids_json, pending_scopes_json, changed_track_count, "
            "cancelled_work_count, updated_at) VALUES (1,'wiped',?,?,0,0,1)",
            (json.dumps([root_id]), json.dumps(msgspec.to_builtins([scope]))),
        )

    await store._write(seed)
    prefs = _RestorePrefs([])
    cache: dict[str, LibraryPolicyResolver | None] = {"resolver": None}

    def get_resolver() -> LibraryPolicyResolver:
        if cache["resolver"] is None:
            cache["resolver"] = LibraryPolicyResolver(
                prefs.get_typed_library_settings()
            )
        return cache["resolver"]

    base = LibraryPolicyService(
        preferences=prefs,
        library_db=None,
        resolver_getter=get_resolver,
        resolver_clearer=lambda: cache.__setitem__("resolver", None),
    )
    reconciliation = LibraryPolicyReconciliationService(store, get_resolver, Mock())
    service = TargetLibraryPolicyService(
        base, reconciliation, store, transition_lock=asyncio.Lock()
    )

    restorable = await service.restorable_roots()
    assert restorable.restorable_roots == [
        LibraryRestorableRoot(root_id=root_id, path="/music", indexed_file_count=2)
    ]
    assert await store.catalog_has_tracks() is True

    saved = await service.restore_roots(
        LibraryRestoreRootsRequest(
            expected_policy_revision=restorable.policy_revision
        )
    )
    saved_roots = [
        (str(root["id"]), str(root["path"]))
        if isinstance(root, dict)
        else (root.id, root.path)
        for root in saved.library_roots
    ]
    assert saved_roots == [(root_id, "/music")]
    pending = await store.get_pending_policy()
    assert pending is not None
    restored_scope = next(
        scope for scope in pending["pending_scopes"] if scope.root_id == root_id
    )
    assert restored_scope.effective_policy == "automatic"


@pytest.mark.asyncio
async def test_restore_roots_prefers_the_pending_policy_path_over_derivation() -> None:
    current = TypedLibrarySettings(
        library_roots=[_root("kept", "/library", "library")]
    )
    base = Mock()
    base.current_settings.return_value = current
    base.current_settings_raw.return_value = current
    base.prepare_change.side_effect = lambda settings, *, expected_policy_revision: (
        LibraryPolicyResolver(settings),
        [],
    )
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    base.get_settings.return_value = LibrarySettingsResponse(
        library_roots=current.library_roots, policy_revision="saved"
    )
    scope = ScanScope(
        root_id="removed",
        scope_id="removed",
        relative_path=".",
        root_path="/music",
        effective_policy="excluded",
        policy_revision="wiped",
    )
    store = AsyncMock()
    store.get_migrated_root_ids.return_value = {"removed"}
    store.get_pending_policy.return_value = {
        "desired_policy_revision": "wiped",
        "pending_scopes": [scope],
        "pending_scope_ids": ["removed"],
    }
    store.get_restorable_root_paths.return_value = {
        "removed": {"path": "/music/Artist One", "indexed_file_count": 12}
    }
    reconciliation = AsyncMock()
    reconciliation.commit_boundary.return_value = {"changed": 0, "cancelled": 0}
    service = TargetLibraryPolicyService(base, reconciliation, store)

    listing = await service.restorable_roots()
    assert listing.restorable_roots == [
        LibraryRestorableRoot(
            root_id="removed", path="/music", indexed_file_count=12
        )
    ]

    await service.restore_roots(
        LibraryRestoreRootsRequest(expected_policy_revision="revision-1")
    )
    sent = base.persist_settings.call_args.args[0]
    assert {root.id: root.path for root in sent.library_roots} == {
        "kept": "/library",
        "removed": "/music",
    }


@pytest.mark.asyncio
async def test_restore_roots_honors_overrides_for_trackless_roots() -> None:
    current = TypedLibrarySettings(library_roots=[])
    base = Mock()
    base.current_settings.return_value = current
    base.current_settings_raw.return_value = current
    base.prepare_change.side_effect = lambda settings, *, expected_policy_revision: (
        LibraryPolicyResolver(settings),
        [],
    )
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    base.get_settings.return_value = LibrarySettingsResponse(
        library_roots=[], policy_revision="saved"
    )
    store = AsyncMock()
    store.get_migrated_root_ids.return_value = {"gone"}
    store.get_pending_policy.return_value = None
    store.get_restorable_root_paths.return_value = {}
    store.catalog_has_tracks.return_value = False
    reconciliation = AsyncMock()
    reconciliation.commit_boundary.return_value = {"changed": 0, "cancelled": 0}
    service = TargetLibraryPolicyService(base, reconciliation, store)

    await service.restore_roots(
        LibraryRestoreRootsRequest(
            expected_policy_revision="revision-1",
            paths={"gone": "/vinyl"},
        )
    )
    sent = base.persist_settings.call_args.args[0]
    assert {root.id: root.path for root in sent.library_roots} == {"gone": "/vinyl"}


@pytest.mark.asyncio
async def test_restored_root_labels_avoid_collisions() -> None:
    current = TypedLibrarySettings(
        library_roots=[_root("kept", "/library", "music")]
    )
    base = Mock()
    base.current_settings.return_value = current
    base.current_settings_raw.return_value = current
    base.prepare_change.side_effect = lambda settings, *, expected_policy_revision: (
        LibraryPolicyResolver(settings),
        [],
    )
    base.rebase_scopes.return_value = []
    base.collapse_scopes.return_value = []
    base.get_settings.return_value = LibrarySettingsResponse(
        library_roots=current.library_roots, policy_revision="saved"
    )
    store = AsyncMock()
    store.get_migrated_root_ids.return_value = {"removed"}
    store.get_pending_policy.return_value = None
    store.get_restorable_root_paths.return_value = {
        "removed": {"path": "/music", "indexed_file_count": 1}
    }
    reconciliation = AsyncMock()
    reconciliation.commit_boundary.return_value = {"changed": 0, "cancelled": 0}
    service = TargetLibraryPolicyService(base, reconciliation, store)

    await service.restore_roots(
        LibraryRestoreRootsRequest(expected_policy_revision="revision-1")
    )
    sent = base.persist_settings.call_args.args[0]
    assert {root.id: root.label for root in sent.library_roots} == {
        "kept": "music",
        "removed": "music (2)",
    }
