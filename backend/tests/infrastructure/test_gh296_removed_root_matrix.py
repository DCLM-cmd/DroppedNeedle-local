"""GH-296 current-HEAD reproduction matrix (real SQLite, real coordinator).

Records, per reporter claim, STILL REPRODUCES vs ALREADY FIXED at the working
tree. This file is evidence for the dossier disposition; it is not a fix."""

import asyncio
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.v1.schemas.library_policies import (
    LibraryRootSettings,
    TypedLibrarySettings,
)
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_inventory_scanner import LibraryInventoryScanner
from services.native.library_policy_service import LibraryPolicyService
from services.native.library_reconciler import LibraryReconciler
from models.library_work import ScanRequest, ScanRun, ScanScope
from services.native.library_scan_coordinator import (
    LibraryIndexer,
    LibraryScanCoordinator,
)
from tests.infrastructure.test_target_scan_lifecycle import _TagReader


def _roots_settings(root_map: dict[str, Path]) -> TypedLibrarySettings:
    ordered = sorted(root_map.items())
    return TypedLibrarySettings(
        library_roots=[
            LibraryRootSettings(
                id=root_id,
                path=str(path),
                label=f"Library {root_id}",
                policy="automatic",
            )
            for index, (root_id, path) in enumerate(ordered, start=1)
        ]
    )


class _Holder:
    def __init__(self, settings: TypedLibrarySettings):
        self.settings = settings


@pytest.fixture
def matrix(tmp_path: Path):
    roots = {
        "root-a": tmp_path / "music" / "a",
        "root-b": tmp_path / "music" / "b",
        "root-c": tmp_path / "music" / "c",
    }
    for root_id, path in roots.items():
        path.mkdir(parents=True)
        for file_index in range(3):
            (path / f"track-{file_index}.flac").write_bytes(
                b"audio-" + bytes([ord(root_id[-1]), file_index])
            )

    database = tmp_path / "target.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users VALUES ('admin')")
    store = NativeLibraryStore(database, threading.Lock())

    holder = _Holder(_roots_settings(roots))

    def resolver_getter():
        from services.native.library_policy_resolver import LibraryPolicyResolver

        return LibraryPolicyResolver(holder.settings)

    cleared = {"n": 0}

    def resolver_clearer():
        cleared["n"] += 1

    preferences = SimpleNamespace(
        get_typed_library_settings=lambda: holder.settings,
        get_typed_library_settings_raw=lambda: holder.settings,
        save_typed_library_settings_if_current=lambda settings, **kwargs: (
            holder.__setattr__("settings", settings)
        ),
    )

    policy_service = LibraryPolicyService(
        preferences, None, resolver_getter, resolver_clearer
    )
    scanner = LibraryInventoryScanner(store, walk_deadline_seconds=30.0)

    def make_coordinator():
        return LibraryScanCoordinator(
            store,
            scanner,
            LibraryIndexer(store, _TagReader()),
            LibraryReconciler(store),
            resolver_getter,
            clock=lambda: 1_800_000_000.0,
        )

    async def full_scan():
        coordinator = make_coordinator()
        await coordinator.request_run(
            ScanRequest(
                kind="incremental",
                trigger="manual",
                policy_revision=resolver_getter().policy_revision,
                scopes=[
                    ScanScope(
                        root_id=root_id,
                        relative_path=".",
                        policy_revision=resolver_getter().policy_revision,
                    )
                    for root_id in sorted(roots)
                ],
                requested_by_user_id="admin",
            )
        )
        run = await store.claim_next_scan_run(now=10)
        assert run is not None
        return await coordinator.run_once(roots)

    return SimpleNamespace(
        store=store,
        roots=roots,
        holder=holder,
        policy_service=policy_service,
        make_coordinator=make_coordinator,
        full_scan=full_scan,
        database=database,
        resolver_getter=resolver_getter,
    )


async def _policy_state(store: NativeLibraryStore) -> dict:
    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM library_policy_state WHERE singleton = 1"
        ).fetchone()
    if row is None:
        return {}
    return {
        "pending_scope_ids": json_loads(row["pending_scope_ids_json"]),
        "desired": row["desired_policy_revision"],
    }


def json_loads(value):
    import json

    return json.loads(value) if value else []


@pytest.mark.asyncio
async def test_gh296_matrix_current_head(matrix) -> None:
    # Baseline: apply all three roots so the catalog + scopes exist.
    finished = await matrix.full_scan()
    assert finished is not None and finished.state == "completed"

    current_revision = matrix.resolver_getter().policy_revision

    # REMOVE root-c through the same settings-save flow the UI uses.
    remaining = {k: v for k, v in matrix.roots.items() if k != "root-c"}
    response = matrix.policy_service.save_settings(
        _roots_settings(remaining),
        expected_policy_revision=current_revision,
    )
    assert response.reconciliation_required is True
    removed_scope_ids = list(response.affected_scope_ids)
    assert removed_scope_ids, "removed root must produce affected scope ids"

    # Reporter's Docker shape: the removed root's MOUNT is gone too.
    import shutil as _shutil

    _shutil.rmtree(matrix.roots["root-c"])

    # CLAIM 1: full incremental scan after removal -> whole run fails
    # ROOT_UNAVAILABLE at discovering (vs skip-and-report).
    failed = await matrix.full_scan()
    assert failed is not None and failed.state == "failed"
    with sqlite3.connect(matrix.database) as connection:
        terminal_code = connection.execute(
            "SELECT terminal_code FROM library_scan_runs WHERE id = ?",
            (failed.id,),
        ).fetchone()[0]
        unavailable_scopes = connection.execute(
            "SELECT COUNT(*) FROM library_scan_run_scopes WHERE run_id = ? "
            "AND discovery_state = 'unavailable'",
            (failed.id,),
        ).fetchone()[0]
    print(f"\nGH-296 claim-1: terminal={terminal_code} "
          f"unavailable-scopes={unavailable_scopes} state={failed.state}")
    assert terminal_code == "ROOT_UNAVAILABLE"
    assert unavailable_scopes >= 1

    # CLAIM 2 (pending-orphan durability): CHANGED AT HEAD. The settings-save
    # flow no longer persists pending_scope_ids_json (policy_state row absent),
    # so there is no durable orphan to survive a non-reconcile failure.
    with sqlite3.connect(matrix.database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM library_policy_state WHERE singleton = 1"
        ).fetchone()
    policy_state_empty = row is None
    assert policy_state_empty

    # CLAIM 3: explicit removed-scope selection is rejected at request build.
    from core.exceptions import ValidationError

    rejected = False
    try:
        await matrix.make_coordinator().request_run(
            ScanRequest(
                kind="incremental",
                trigger="manual",
                policy_revision=matrix.resolver_getter().policy_revision,
                scopes=[
                    ScanScope(
                        root_id="root-c",
                        relative_path=".",
                        policy_revision=matrix.resolver_getter().policy_revision,
                    )
                ],
            )
        )
    except ValidationError:
        rejected = True
    # FINDING: the coordinator/store layer does NOT validate existence; the
    # 400 lives in the route's _selected_scopes only. The root-c run queues
    # here and poisons every subsequent claim (reporter mechanism).
    print(f"GH-296 claim-3 removed-root request rejected={rejected}")

    # CLAIM 4 (reporter escape hatch): at HEAD the empty-scope reconcile
    # request is rejected by validation ("Select at least one library scope"),
    # so the v2.6.0 workaround no longer applies as-is.
    rejected_empty = False
    coordinator = matrix.make_coordinator()
    try:
        await coordinator.request_run(
            ScanRequest(
                kind="policy_reconcile",
                trigger="manual",
                policy_revision=matrix.resolver_getter().policy_revision,
                scopes=[],
            )
        )
    except ValidationError as error:
        rejected_empty = "scope" in str(error).lower()
    assert rejected_empty

    # FINDING: claim-3's root-c run IS queued and will poison claims.
    poisoned = await matrix.store.claim_next_scan_run(now=40)
    assert poisoned is not None
    poisoned_scopes = []
    with sqlite3.connect(matrix.database) as connection:
        poisoned_scopes = [
            row[0]
            for row in connection.execute(
                "SELECT root_id FROM library_scan_run_scopes WHERE run_id=?",
                (poisoned.id,),
            ).fetchall()
        ]
    assert poisoned_scopes == ["root-c"], (
        f"queued removed-root scope reproduces the poisoned queue: {poisoned_scopes}"
    )
    # queued -> cancelled is a legal transition; the poisoned run leaves the
    # claim queue without ever touching the filesystem.
    await matrix.store.transition_scan_run(
        poisoned.id,
        expected_state=poisoned.state,  # discovering after claim
        expected_revision=poisoned.row_revision,
        new_state="failed",
        terminal_code="ROOT_UNAVAILABLE",
        now=50,
    )

    # Scanning resumes over surviving roots once the poisoned run is cleared.
    coordinator2 = matrix.make_coordinator()
    resume_request = ScanRequest(
        kind="incremental",
        trigger="manual",
        policy_revision=matrix.resolver_getter().policy_revision,
        scopes=[
            ScanScope(
                root_id=root_id,
                relative_path=".",
                policy_revision=matrix.resolver_getter().policy_revision,
            )
            for root_id in ("root-a", "root-b")
        ],
    )
    print("RESUME REQUEST SCOPES:", [s.root_id for s in resume_request.scopes])
    print("HOLDER ROOTS NOW:", [r.id for r in matrix.holder.settings.library_roots])
    print("RESOLVER ROOTS NOW:", [
        r.id for r in matrix.resolver_getter().settings.library_roots
    ])
    created = await coordinator2.request_run(resume_request)
    created_scopes = [
        s.root_id
        for _, s in [
            (None, SimpleNamespace(root_id=row[0]))
            for row in sqlite3.connect(matrix.database).execute(
                "SELECT root_id FROM library_scan_run_scopes WHERE run_id=?",
                (created.run_id,),
            ).fetchall()
        ]
    ]
    print("CREATED RUN SCOPES:", created_scopes)
    resumed_run = await matrix.store.claim_next_scan_run(now=40)
    assert resumed_run is not None
    finished_resume = await coordinator2.run_once(
        {k: v for k, v in matrix.roots.items() if k != "root-c"}
    )
    print("RESUME RUN:", resumed_run.id, resumed_run.kind,
          resumed_run.trigger)
    if finished_resume is None or finished_resume.state != "completed":
        with sqlite3.connect(matrix.database) as connection:
            fails = connection.execute(
                "SELECT root_id, relative_path, error_code FROM "
                "library_scan_run_scopes WHERE run_id=?",
                (resumed_run.id,),
            ).fetchall()
        raise AssertionError(
            f"resume failed: state={finished_resume.state if finished_resume else None} "
            f"scopes={fails}"
        )

    # CHANGED AT HEAD: no durable pending_scope_ids_json exists anymore, so
    # the v2.6.0 orphan cannot persist; record emptiness as evidence.
    state_after_workaround = await _policy_state(matrix.store)
    print(f"GH-296 workaround: policy-state-after={state_after_workaround}")
    assert not state_after_workaround.get("pending_scope_ids")
