import json
import sqlite3
import threading
from pathlib import Path

import pytest

from api.v1.schemas.library_policies import (
    LibraryRootSettings,
    TypedLibrarySettings,
)
from core import dependencies as deps
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.compat.target_scan_service import TargetCompatScanService
from services.native.library_indexer import LibraryIndexer
from services.native.library_inventory_scanner import LibraryInventoryScanner
from services.native.library_policy_resolver import LibraryPolicyResolver
from services.native.library_reconciler import LibraryReconciler
from services.native.library_scan_coordinator import LibraryScanCoordinator
from tests.compat.conftest import subsonic_query


def _body(response):
    return json.loads(response.content)["subsonic-response"]


class _TagReader:
    def read_tags(self, path: Path):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_scan_status_is_available_to_authenticated_users(compat_env):
    compat_env.scan.status.return_value = (True, 37)
    body = _body(compat_env.client.get(
        "/subsonic/rest/getScanStatus",
        params=subsonic_query(compat_env.secret, "alice"),
    ))

    assert body["scanStatus"] == {"scanning": True, "count": 37}


@pytest.mark.asyncio
async def test_scan_start_is_admin_only(compat_env):
    body = _body(compat_env.client.post(
        "/subsonic/rest/startScan",
        params=subsonic_query(compat_env.secret, "alice"),
    ))

    assert body["status"] == "failed"
    assert body["error"]["code"] == 50
    compat_env.scan.start.assert_not_awaited()

    await compat_env.auth_store.update_user_role("user-bob", "admin")
    _record, secret = await compat_env.app_passwords.create("user-bob", "admin scan")
    allowed = _body(compat_env.client.post(
        "/subsonic/rest/startScan",
        params=subsonic_query(secret, "bob"),
    ))
    assert allowed["scanStatus"] == {"scanning": True, "count": 0}
    compat_env.scan.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_start_creates_one_run_and_second_start_coalesces(
    compat_env, tmp_path
):
    """S-04/X-03: startScan fans out to
    ``request_run(ScanRequest(kind="incremental", trigger="subsonic", ...))`` -
    exactly one run is created and a second start coalesces onto it (temp-DB
    store, mirroring the R3 live probe). The queued-state ``(False, 0)``
    status is by design: nothing has inspected anything yet."""
    db_path = tmp_path / "target.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    store = NativeLibraryStore(db_path=db_path, write_lock=threading.Lock())
    root = tmp_path / "music"
    root.mkdir()
    resolver = LibraryPolicyResolver(
        TypedLibrarySettings(
            library_roots=[
                LibraryRootSettings(
                    id="root-a", path=str(root), label="Library", policy="automatic"
                )
            ],
            enabled=True,
        )
    )
    coordinator = LibraryScanCoordinator(
        store,
        LibraryInventoryScanner(store),
        LibraryIndexer(store, _TagReader()),
        LibraryReconciler(store),
        lambda: resolver,
    )
    service = TargetCompatScanService(coordinator, lambda: resolver)
    compat_env.app.dependency_overrides[deps.get_target_compat_scan_service] = (
        lambda: service
    )

    await compat_env.auth_store.update_user_role("user-bob", "admin")
    _record, secret = await compat_env.app_passwords.create("user-bob", "admin scan")
    first = _body(compat_env.client.post(
        "/subsonic/rest/startScan",
        params=subsonic_query(secret, "bob"),
    ))
    second = _body(compat_env.client.post(
        "/subsonic/rest/startScan",
        params=subsonic_query(secret, "bob"),
    ))

    assert first["scanStatus"] == {"scanning": True, "count": 0}
    assert second["scanStatus"] == {"scanning": True, "count": 0}
    runs = await store.list_current_scan_runs()
    assert len(runs) == 1
    assert runs[0].kind == "incremental"
    assert runs[0].trigger == "subsonic"
    status = _body(compat_env.client.get(
        "/subsonic/rest/getScanStatus",
        params=subsonic_query(secret, "bob"),
    ))
    assert status["scanStatus"] == {"scanning": False, "count": 0}
