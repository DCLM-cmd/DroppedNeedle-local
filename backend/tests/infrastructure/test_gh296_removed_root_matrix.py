"""GH-296 removed-root skip-and-report and removal-reconciliation suite.

Origin: the committed current-HEAD reproduction matrix (e1500f3) recorded,
per reporter claim, what still reproduced at working-tree HEAD. The
owner-approved skip-and-report design (2026-08-23) turned that matrix into
this behavioral suite for the fix, with two corrections to the original
evidence:

- Claim 1 still reproduced at HEAD through the REAL settings flow
  (``TargetLibraryPolicyService.save_settings`` -> ``policy_apply``): a
  reconcile run whose only frozen scope belonged to the removed root failed
  the WHOLE run with ``terminal_code=ROOT_UNAVAILABLE`` at discovering.
- Claim 2 ("no durable pending_scope_ids_json") was an artifact of the
  original matrix exercising ``LibraryPolicyService.save_settings``, which
  never persists pending state. The route service
  (``TargetLibraryPolicyService``) DOES write the durable
  ``library_policy_state`` row on removal, and it survived every ordinary
  scan because only a completed ``policy_reconcile`` run could clear it -
  the permanently unreconcilable banner from v2.6.0 is alive at HEAD on the
  real path whenever the removed mount is gone.
- Claim 3 reproduced verbatim: coordinator/store ``request_run`` accepted
  scopes for unconfigured roots (the poisoned queue vector).

- Skip-and-report: an unresolvable or missing-path scope is recorded
  honestly (failure row + ``discovery_state='unavailable'`` +
  ``error_code='ROOT_UNAVAILABLE'``) while remaining scopes complete; the
  run reaches ``completed`` through the existing state machine with no new
  states, and the skip stays visible in run detail, failure rows, and the
  diagnostics export. A run whose EVERY scope proved unreachable still
  terminates honestly as ``failed``/``ROOT_UNAVAILABLE`` - never silently
  green. WALK_TIMEOUT behavior is intentionally untouched.
- Request-time validation: non-policy requests referencing unconfigured
  roots are rejected with the typed domain error at the coordinator boundary;
  frozen policy-apply scopes stay exempt (F-TARGETCATALOG-02 carrier).
- Removal converges: a completed reconcile settles its applied/unavailable
  frozen scope ids, and a failed reconcile whose every scope proved
  ROOT_UNAVAILABLE at the desired revision settles them too, so
  ``reconciliation_required`` returns to false through supported flows with
  no out-of-band SQL and restore/remove cycles converge.

F-SCAN-05 remains DEFERRED: no durable walk cursors anywhere here.
"""

import shutil
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.v1.schemas.library_policies import (
    LibraryRootSettings,
    TypedLibrarySettings,
)
from core.exceptions import ValidationError
from infrastructure.persistence.native_library_store import NativeLibraryStore
from models.library_work import ScanRequest, ScanScope
from services.native.library_inventory_scanner import LibraryInventoryScanner
from services.native.library_policy_reconciliation_service import (
    LibraryPolicyReconciliationService,
)
from services.native.library_policy_service import LibraryPolicyService
from services.native.library_reconciler import LibraryReconciler
from services.native.library_scan_coordinator import (
    LibraryIndexer,
    LibraryScanCoordinator,
)
from services.native.target_library_policy_service import TargetLibraryPolicyService
from tests.infrastructure.test_target_scan_lifecycle import _TagReader


def _roots_settings(root_map: dict[str, Path]) -> TypedLibrarySettings:
    return TypedLibrarySettings(
        library_roots=[
            LibraryRootSettings(
                id=root_id,
                path=str(path),
                label=f"Library {root_id}",
                policy="automatic",
            )
            for root_id, path in sorted(root_map.items())
        ]
    )


def _seed_files(path: Path, count: int, tag: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (path / f"track-{index}.flac").write_bytes(f"audio-{tag}-{index}".encode())


class _Holder:
    def __init__(self, settings: TypedLibrarySettings):
        self.settings = settings


@pytest.fixture
def gh296(tmp_path: Path):
    roots = {
        "root-a": tmp_path / "music" / "a",
        "root-b": tmp_path / "music" / "b",
        "root-c": tmp_path / "music" / "c",
    }
    for root_id, path in roots.items():
        _seed_files(path, 3, root_id)

    database = tmp_path / "target.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users VALUES ('admin')")
    store = NativeLibraryStore(database, threading.Lock())

    holder = _Holder(_roots_settings(roots))

    def resolver_getter():
        from services.native.library_policy_resolver import LibraryPolicyResolver

        return LibraryPolicyResolver(holder.settings)

    preferences = SimpleNamespace(
        get_typed_library_settings=lambda: holder.settings,
        get_typed_library_settings_raw=lambda: holder.settings,
        save_typed_library_settings_if_current=lambda settings, **kwargs: (
            holder.__setattr__("settings", settings)
        ),
    )

    policy_service = LibraryPolicyService(preferences, None, resolver_getter, lambda: None)
    scanner = LibraryInventoryScanner(store, walk_deadline_seconds=30.0)
    coordinator = LibraryScanCoordinator(
        store,
        scanner,
        LibraryIndexer(store, _TagReader()),
        LibraryReconciler(store),
        resolver_getter,
        clock=lambda: 1_800_000_000.0,
    )
    reconciliation = LibraryPolicyReconciliationService(
        store, resolver_getter, coordinator
    )
    target_policy = TargetLibraryPolicyService(
        policy_service, reconciliation, store
    )

    async def full_scan(root_map: dict[str, Path]):
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
                    for root_id in sorted(root_map)
                ],
                requested_by_user_id="admin",
            )
        )
        claimed = await store.claim_next_scan_run(now=10)
        assert claimed is not None
        return await coordinator.run_once(root_map)

    async def run_apply(scope_ids: list[str], root_map: dict[str, Path]):
        """Apply frozen pending scopes through the real route-service chain."""
        result = await reconciliation.apply(
            scope_ids,
            expected_policy_revision=resolver_getter().policy_revision,
            requested_by_user_id="admin",
        )
        claimed = await store.claim_next_scan_run(now=20)
        assert claimed is not None
        return result, await coordinator.run_once(root_map)

    def query(sql: str, parameters: tuple = ()):
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, parameters)]

    return SimpleNamespace(
        store=store,
        database=database,
        roots=roots,
        holder=holder,
        policy_service=policy_service,
        target_policy=target_policy,
        reconciliation=reconciliation,
        coordinator=coordinator,
        full_scan=full_scan,
        run_apply=run_apply,
        query=query,
        resolver_getter=resolver_getter,
    )


def _pending_scope_ids(harness) -> set[str]:
    rows = harness.query(
        "SELECT pending_scope_ids_json FROM library_policy_state WHERE singleton = 1"
    )
    if not rows:
        return set()
    import json

    return set(json.loads(rows[0]["pending_scope_ids_json"]))


async def _remove_root_c(harness) -> None:
    """Remove root-c through the exact settings service the UI uses."""
    remaining = {
        root_id: path for root_id, path in harness.roots.items() if root_id != "root-c"
    }
    response = await harness.target_policy.save_settings(
        _roots_settings(remaining),
        expected_policy_revision=harness.resolver_getter().policy_revision,
    )
    assert response.reconciliation_required is True
    assert set(response.affected_scope_ids) >= {"root-c"}
    # Real-flow durability: the frozen removed-root scope persists until a
    # reconcile run settles it (corrects the original matrix claim 2).
    assert "root-c" in _pending_scope_ids(harness)
    shutil.rmtree(harness.roots["root-c"])


@pytest.mark.asyncio
async def test_removed_root_scope_is_skipped_and_reported_while_healthy_scopes_complete(
    gh296,
) -> None:
    finished = await gh296.full_scan(gh296.roots)
    assert finished is not None and finished.state == "completed"

    # One save removes root-c and adds a healthy root-d, so the frozen
    # transition carries both an unreachable and a reachable scope.
    root_d = gh296.roots["root-c"].parent / "d"
    _seed_files(root_d, 2, "d")
    settings = {k: v for k, v in gh296.roots.items() if k != "root-c"}
    settings["root-d"] = root_d
    saved = await gh296.target_policy.save_settings(
        _roots_settings(settings),
        expected_policy_revision=gh296.resolver_getter().policy_revision,
    )
    assert saved.reconciliation_required is True
    assert set(saved.affected_scope_ids) == {"root-c", "root-d"}
    shutil.rmtree(gh296.roots["root-c"])

    _, outcome = await gh296.run_apply(["root-c", "root-d"], settings)

    # The run completes over the healthy scope instead of failing wholesale.
    assert outcome.state == "completed"
    assert outcome.terminal_code is None
    scopes = {
        row["root_id"]: (row["discovery_state"], row["error_code"])
        for row in gh296.query(
            "SELECT root_id, discovery_state, error_code FROM "
            "library_scan_run_scopes WHERE run_id = ?",
            (outcome.id,),
        )
    }
    assert scopes["root-d"] == ("completed", None)
    assert scopes["root-c"] == ("unavailable", "ROOT_UNAVAILABLE")
    failures = gh296.query(
        "SELECT failure_code, failure_detail, phase FROM library_scan_failures "
        "WHERE run_id = ? AND root_id = 'root-c'",
        (outcome.id,),
    )
    assert len(failures) == 1
    assert failures[0]["failure_code"] == "ROOT_UNAVAILABLE"
    assert failures[0]["phase"] == "discovering"
    assert "missing" in failures[0]["failure_detail"]
    # The healthy scope was actually discovered and indexed.
    indexed = gh296.query(
        "SELECT COUNT(*) AS n FROM library_scan_inventory WHERE run_id = ? "
        "AND root_id = 'root-d'",
        (outcome.id,),
    )[0]["n"]
    assert indexed == 2

    # Completion settles the whole frozen transition, including the skipped
    # scope, so the banner clears through a supported flow.
    refreshed = await gh296.target_policy.get_settings()
    assert refreshed.reconciliation_required is False
    assert _pending_scope_ids(gh296) == set()


@pytest.mark.asyncio
async def test_run_whose_every_scope_is_unreachable_fails_and_settles_pending(
    gh296,
) -> None:
    finished = await gh296.full_scan(gh296.roots)
    assert finished is not None and finished.state == "completed"

    await _remove_root_c(gh296)
    _, outcome = await gh296.run_apply(
        ["root-c"], {k: v for k, v in gh296.roots.items() if k != "root-c"}
    )

    # Never silently green: the honest terminal outcome stands even though
    # the pending transition settles.
    assert outcome.state == "failed"
    assert outcome.terminal_code == "ROOT_UNAVAILABLE"
    scopes = gh296.query(
        "SELECT discovery_state, error_code FROM library_scan_run_scopes "
        "WHERE run_id = ?",
        (outcome.id,),
    )
    assert scopes == [{"discovery_state": "unavailable", "error_code": "ROOT_UNAVAILABLE"}]
    failures = gh296.query(
        "SELECT failure_code FROM library_scan_failures WHERE run_id = ?",
        (outcome.id,),
    )
    assert [row["failure_code"] for row in failures] == ["ROOT_UNAVAILABLE"]
    assert (
        gh296.query(
            "SELECT COUNT(*) AS n FROM library_scan_inventory WHERE run_id = ?",
            (outcome.id,),
        )[0]["n"]
        == 0
    )

    # GH-296 convergence: the failed reconcile proved every frozen scope
    # unreachable at the desired revision, so reconciliation_required
    # returns to false with no out-of-band SQL.
    refreshed = await gh296.target_policy.get_settings()
    assert refreshed.reconciliation_required is False
    assert _pending_scope_ids(gh296) == set()


@pytest.mark.asyncio
async def test_poisoned_request_for_unconfigured_root_is_rejected(gh296) -> None:
    finished = await gh296.full_scan(gh296.roots)
    assert finished is not None and finished.state == "completed"

    await _remove_root_c(gh296)
    revision = gh296.resolver_getter().policy_revision

    with pytest.raises(ValidationError, match="no longer exist"):
        await gh296.coordinator.request_run(
            ScanRequest(
                kind="incremental",
                trigger="manual",
                policy_revision=revision,
                scopes=[
                    ScanScope(
                        root_id="root-c",
                        relative_path=".",
                        policy_revision=revision,
                    )
                ],
                requested_by_user_id="admin",
            )
        )
    # Nothing was queued: the poisoned-queue vector dies at the boundary.
    assert (
        gh296.query("SELECT COUNT(*) AS n FROM library_scan_runs")[0]["n"] == 1
    )

    # Healthy selections are still accepted after the rejection.
    accepted = await gh296.coordinator.request_run(
        ScanRequest(
            kind="incremental",
            trigger="manual",
            policy_revision=revision,
            scopes=[
                ScanScope(
                    root_id="root-a",
                    relative_path=".",
                    policy_revision=revision,
                )
            ],
            requested_by_user_id="admin",
        )
    )
    assert accepted.disposition in {"started", "queued", "expanded", "coalesced"}


@pytest.mark.asyncio
async def test_restore_and_remove_cycles_converge_without_manual_steps(gh296) -> None:
    finished = await gh296.full_scan(gh296.roots)
    assert finished is not None and finished.state == "completed"

    survivors = {k: v for k, v in gh296.roots.items() if k != "root-c"}

    for cycle in range(2):
        # REMOVE through the settings flow; the mount vanishes with it.
        await _remove_root_c(gh296)
        _, removal_outcome = await gh296.run_apply(["root-c"], survivors)
        assert removal_outcome.state == "failed"
        assert removal_outcome.terminal_code == "ROOT_UNAVAILABLE"
        refreshed = await gh296.target_policy.get_settings()
        assert refreshed.reconciliation_required is False
        assert _pending_scope_ids(gh296) == set()

        # RESTORE through the ordinary settings save (the supported flow for
        # native roots; restorable_roots covers only legacy-migrated ones);
        # the directory comes back first so the restored scope is walkable.
        _seed_files(gh296.roots["root-c"], 3, f"c{cycle}")
        restored = await gh296.target_policy.save_settings(
            _roots_settings(gh296.roots),
            expected_policy_revision=gh296.resolver_getter().policy_revision,
        )
        assert restored.reconciliation_required is True
        assert {root["id"] for root in restored.library_roots} == set(gh296.roots)
        _, restore_outcome = await gh296.run_apply(["root-c"], gh296.roots)
        assert restore_outcome.state == "completed"
        refreshed = await gh296.target_policy.get_settings()
        assert refreshed.reconciliation_required is False
        assert _pending_scope_ids(gh296) == set()

    # Ordinary scanning over surviving roots stays healthy after the cycles.
    final = await gh296.full_scan(survivors)
    assert final is not None and final.state == "completed"
