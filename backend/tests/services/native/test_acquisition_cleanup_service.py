import os
import sqlite3
import threading
import hashlib
from pathlib import Path
from types import SimpleNamespace

import msgspec
import pytest

from infrastructure.persistence.download_store import DownloadStore
from infrastructure.service_health import service_health
from models.audio import AudioInfo, AudioTag
from models.library_management import (
    LibraryManagementImportBundle,
    LibraryManagementImportFile,
)
from repositories.protocols.download_client import (
    DownloadMaterialization,
    TaskHandle,
)
from services.native.acquisition_cleanup_service import AcquisitionCleanupService
from services.native import acquisition_cleanup_service as cleanup_module


class _LibraryStore:
    def __init__(self) -> None:
        self.bundles: dict[str, str] = {}
        self.records: dict[str, SimpleNamespace] = {}
        self.journals: dict[str, list[SimpleNamespace]] = {}
        self.task_bundles: dict[str, list[SimpleNamespace]] = {}
        self.committed_fingerprints: set[str] = set()

    async def committed_import_source_fingerprints(self, fingerprints):
        return {value for value in fingerprints if value in self.committed_fingerprints}

    async def get_library_management_import_bundle(self, bundle_id: str):
        if bundle_id in self.records:
            return self.records[bundle_id]
        state = self.bundles.get(bundle_id)
        return SimpleNamespace(state=state) if state else None

    async def list_library_management_import_journals(self, bundle_id: str):
        return self.journals.get(bundle_id, [])

    async def list_acquisition_import_bundles_for_download_task(self, task_id: str):
        return self.task_bundles.get(task_id, [])


class _Client:
    def __init__(self, materialization: DownloadMaterialization) -> None:
        self.materialization = materialization
        self.aborted = 0
        self.discarded = 0
        self.discard_error = False

    async def inspect_materialization(
        self, handle: TaskHandle
    ) -> DownloadMaterialization:
        return self.materialization

    async def abort(self, handle: TaskHandle) -> bool:
        self.aborted += 1
        return True

    async def discard_client_artifacts(self, handle: TaskHandle) -> bool:
        self.discarded += 1
        if self.discard_error:
            raise RuntimeError("history unavailable")
        return True


def _store(tmp_path: Path) -> DownloadStore:
    path = tmp_path / "library.db"
    store = DownloadStore(path, threading.Lock())
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS auth_users "
            "(id TEXT PRIMARY KEY, username TEXT, role TEXT)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO auth_users VALUES ('user-a','alice','user')"
        )
        connection.commit()
    finally:
        connection.close()
    return store


async def _attempt(
    store: DownloadStore,
    root: Path,
    *,
    task_id: str = "a" * 32,
    source: str = "usenet",
    workspace: Path | None = None,
    paths: list[Path] | None = None,
    bundle_ids: list[str] | None = None,
):
    job_name = f"droppedneedle-{task_id}-0" if source == "usenet" else ""
    attempt = await store.create_download_attempt(
        task_id=task_id,
        source=source,
        candidate_index=0,
        job_name=job_name,
        handle=TaskHandle(
            source=source,
            job_name=job_name,
            username="peer" if source == "soulseek" else "",
            filenames=[path.name for path in paths or []],
        ),
        now=1.0,
    )
    return await store.schedule_download_attempt_cleanup(
        attempt.id,
        disposition="discard",
        publisher_bundle_ids=bundle_ids or [],
        now=2.0,
    )


@pytest.fixture(autouse=True)
def _clear_health():
    service_health.clear()
    yield
    service_health.clear()


@pytest.mark.asyncio
async def test_completed_workspace_is_removed_after_barriers_clear(tmp_path: Path):
    root = tmp_path / "sab"
    workspace = root / "audio" / f"droppedneedle-{'a' * 32}-0"
    nested = workspace / "Disc 1"
    nested.mkdir(parents=True)
    for name in ("01.flac", "02-unmatched.flac", "cover.jpg", "album.nfo", "list.m3u"):
        (nested / name).write_bytes(b"source")
    library_copy = tmp_path / "library" / "01.flac"
    library_copy.parent.mkdir()
    library_copy.write_bytes(b"library")

    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace, bundle_ids=["bundle"])
    library = _LibraryStore()
    library.bundles["bundle"] = "completed"
    client = _Client(
        DownloadMaterialization(
            state="completed",
            nzo_id="nzo-1",
            remote_storage=f"/remote/audio/{workspace.name}",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, library, lambda source: client, lambda: root
    )

    assert await service.cleanup_now(attempt.id, worker_id="test") is True

    assert not workspace.exists()
    assert library_copy.read_bytes() == b"library"
    assert (await store.get_download_attempt(attempt.id)).state == "complete"
    assert client.discarded == 1


@pytest.mark.asyncio
async def test_history_failure_resumes_from_workspace_removed(tmp_path: Path):
    now = [10.0]
    root = tmp_path / "sab"
    workspace = root / f"droppedneedle-{'a' * 32}-0"
    workspace.mkdir(parents=True)
    (workspace / "track.flac").write_bytes(b"x")
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            remote_storage=f"/remote/{workspace.name}",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=True,
        )
    )
    client.discard_error = True
    service = AcquisitionCleanupService(
        store,
        _LibraryStore(),
        lambda source: client,
        lambda: root,
        clock=lambda: now[0],
    )

    await service.cleanup_now(attempt.id, worker_id="first")
    failed = await store.get_download_attempt(attempt.id)
    assert not workspace.exists()
    assert failed.state == "workspace_removed"
    assert failed.cleanup_failures == 1

    client.discard_error = False
    now[0] = failed.next_retry_at
    await service.cleanup_now(attempt.id, worker_id="second")
    assert (await store.get_download_attempt(attempt.id)).state == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["root", "outside", "symlink"])
async def test_unsafe_workspace_evidence_is_preserved(tmp_path: Path, case: str):
    root = tmp_path / "sab"
    root.mkdir()
    job_name = f"droppedneedle-{'a' * 32}-0"
    outside = tmp_path / job_name
    outside.mkdir()
    if case == "root":
        workspace = root
    elif case == "outside":
        workspace = outside
    else:
        workspace = root / job_name
        workspace.symlink_to(outside, target_is_directory=True)
    (outside / "keep.flac").write_bytes(b"keep")
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            remote_storage=f"/remote/{job_name}",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )

    await service.cleanup_now(attempt.id, worker_id="test")

    assert (await store.get_download_attempt(attempt.id)).state == "needs_attention"
    assert (outside / "keep.flac").exists()
    assert service_health.is_degraded("acquisition_cleanup", "source files")


@pytest.mark.asyncio
async def test_symlinked_mount_root_is_refused(tmp_path: Path):
    real_root = tmp_path / "real-sab"
    job_name = f"droppedneedle-{'a' * 32}-0"
    workspace = real_root / job_name
    workspace.mkdir(parents=True)
    source = workspace / "track.flac"
    source.write_bytes(b"keep")
    root = tmp_path / "sab"
    root.symlink_to(real_root, target_is_directory=True)
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=root / job_name)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            workspace_path=str(root / job_name),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )

    await service.cleanup_now(attempt.id, worker_id="test")

    assert source.read_bytes() == b"keep"
    assert (await store.get_download_attempt(attempt.id)).state == "needs_attention"


@pytest.mark.asyncio
async def test_unhealthy_mount_retries_and_missing_workspace_is_idempotent(
    tmp_path: Path,
):
    root = tmp_path / "sab"
    root.mkdir()
    workspace = root / f"droppedneedle-{'a' * 32}-0"
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            remote_storage=f"/remote/{workspace.name}",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=False,
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )
    await service.cleanup_now(attempt.id, worker_id="unhealthy")
    pending = await store.get_download_attempt(attempt.id)
    assert pending.state == "cleanup_pending"
    assert pending.cleanup_failures == 1

    client.materialization.mount_healthy = True
    await store.transition_download_attempt(
        attempt.id,
        expected_row_revision=pending.row_revision,
        new_state="cleanup_pending",
        next_retry_at=0.0,
        now=20.0,
    )
    await service.cleanup_now(attempt.id, worker_id="healthy")
    assert (await store.get_download_attempt(attempt.id)).state == "complete"


@pytest.mark.asyncio
async def test_missing_history_cannot_authorize_a_persisted_workspace(tmp_path: Path):
    now = [10.0]
    root = tmp_path / "sab"
    workspace = root / f"droppedneedle-{'a' * 32}-0"
    workspace.mkdir(parents=True)
    source = workspace / "track.flac"
    source.write_bytes(b"keep")
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            remote_storage=f"/remote/{workspace.name}",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=False,
        )
    )
    service = AcquisitionCleanupService(
        store,
        _LibraryStore(),
        lambda source: client,
        lambda: root,
        clock=lambda: now[0],
    )

    await service.cleanup_now(attempt.id, worker_id="record-evidence")
    pending = await store.get_download_attempt(attempt.id)
    client.materialization = DownloadMaterialization(
        state="missing", mount_root=str(root), mount_healthy=True
    )
    now[0] = pending.next_retry_at
    await service.cleanup_now(attempt.id, worker_id="history-gone")

    assert source.read_bytes() == b"keep"
    assert (await store.get_download_attempt(attempt.id)).state == "needs_attention"


@pytest.mark.asyncio
async def test_unresolved_enqueue_identity_waits_before_declaring_absence(
    tmp_path: Path,
):
    now = [10.0]
    root = tmp_path / "sab"
    root.mkdir()
    store = _store(tmp_path)
    attempt = await _attempt(store, root)
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = AcquisitionCleanupService(
        store,
        _LibraryStore(),
        lambda source: client,
        lambda: root,
        clock=lambda: now[0],
    )

    for index in range(4):
        await service.cleanup_now(attempt.id, worker_id=f"missing-{index}")
        current = await store.get_download_attempt(attempt.id)
        assert current.state == "cleanup_pending"
        now[0] = current.next_retry_at

    await service.cleanup_now(attempt.id, worker_id="stabilized")
    assert (await store.get_download_attempt(attempt.id)).state == "complete"


@pytest.mark.asyncio
async def test_slskd_cleanup_unlinks_only_exact_files(tmp_path: Path):
    root = tmp_path / "slskd"
    album = root / "shared-album"
    album.mkdir(parents=True)
    source = album / "requested.flac"
    sibling = album / "other.flac"
    source.write_bytes(b"source")
    sibling.write_bytes(b"other")
    store = _store(tmp_path)
    attempt = await _attempt(store, root, source="soulseek", paths=[source])
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            file_paths=[str(source)],
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )

    await service.cleanup_now(attempt.id, worker_id="test")

    assert not source.exists()
    assert sibling.read_bytes() == b"other"
    assert album.is_dir()


@pytest.mark.asyncio
async def test_slskd_retry_accepts_remaining_subset_after_partial_unlink(
    tmp_path: Path, monkeypatch
):
    now = [10.0]
    root = tmp_path / "slskd"
    root.mkdir()
    first = root / "first.flac"
    second = root / "second.flac"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    store = _store(tmp_path)
    attempt = await _attempt(store, root, source="soulseek", paths=[first, second])
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            file_paths=[str(first), str(second)],
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store,
        _LibraryStore(),
        lambda source: client,
        lambda: root,
        clock=lambda: now[0],
    )
    original_unlink = cleanup_module._unlink_file_safely

    def fail_on_second(mount: Path, source: Path, expected_sha256: str | None) -> None:
        if source == second:
            raise OSError("temporary failure")
        original_unlink(mount, source, expected_sha256)

    monkeypatch.setattr(cleanup_module, "_unlink_file_safely", fail_on_second)
    await service.cleanup_now(attempt.id, worker_id="partial")
    pending = await store.get_download_attempt(attempt.id)
    assert not first.exists()
    assert second.exists()
    assert pending.state == "cleanup_pending"

    monkeypatch.setattr(cleanup_module, "_unlink_file_safely", original_unlink)
    client.materialization.file_paths = [str(second)]
    now[0] = pending.next_retry_at
    await service.cleanup_now(attempt.id, worker_id="retry")

    assert not second.exists()
    assert (await store.get_download_attempt(attempt.id)).state == "complete"


@pytest.mark.asyncio
async def test_slskd_retry_preserves_replacement_at_reused_source_path(
    tmp_path: Path,
):
    now = [10.0]
    root = tmp_path / "slskd"
    root.mkdir()
    source = root / "requested.flac"
    source.write_bytes(b"original transfer")
    store = _store(tmp_path)
    attempt = await _attempt(store, root, source="soulseek", paths=[source])
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            file_paths=[str(source)],
            mount_healthy=True,
        )
    )
    client.discard_error = True
    service = AcquisitionCleanupService(
        store,
        _LibraryStore(),
        lambda source: client,
        lambda: root,
        clock=lambda: now[0],
    )

    await service.cleanup_now(attempt.id, worker_id="first")
    pending = await store.get_download_attempt(attempt.id)
    assert pending.state == "cleanup_pending"
    assert pending.materialized_fingerprints == {
        str(source.resolve()): hashlib.sha256(b"original transfer").hexdigest()
    }
    assert not source.exists()

    source.write_bytes(b"unrelated replacement")
    client.discard_error = False
    now[0] = pending.next_retry_at
    await service.cleanup_now(attempt.id, worker_id="retry")

    attention = await store.get_download_attempt(attempt.id)
    assert attention.state == "needs_attention"
    assert attention.error_code == "source_file_fingerprint_changed"
    assert source.read_bytes() == b"unrelated replacement"
    assert client.discarded == 1


@pytest.mark.asyncio
async def test_slskd_first_cleanup_uses_publisher_fingerprint_for_reused_path(
    tmp_path: Path,
):
    root = tmp_path / "slskd"
    root.mkdir()
    source = root / "requested.flac"
    source.write_bytes(b"unrelated replacement")
    original_fingerprint = hashlib.sha256(b"published source").hexdigest()
    bundle = LibraryManagementImportBundle(
        idempotency_key="cleanup-source-evidence",
        origin="acquisition",
        policy_revision="policy-1",
        files=(
            LibraryManagementImportFile(
                ordinal=0,
                input_path=str(source),
                destination_root_id="root-1",
                destination_relative_path="Artist/Album/01 Track.flac",
                tag=AudioTag(
                    title="Track",
                    artist="Artist",
                    album="Album",
                    track_number=1,
                ),
                info=AudioInfo(
                    duration_seconds=180.0,
                    bitrate=900,
                    sample_rate=44_100,
                    channels=2,
                    file_format="flac",
                    file_size_bytes=len(b"published source"),
                    bit_depth=16,
                ),
                release_group_mbid=None,
                release_mbid=None,
                recording_mbid=None,
                confidence=1.0,
                source="download",
            ),
        ),
    )
    request_json = msgspec.json.encode(bundle).decode()
    library = _LibraryStore()
    library.records["bundle"] = SimpleNamespace(
        state="completed",
        request_json=request_json,
        request_hash=hashlib.sha256(request_json.encode()).hexdigest(),
    )
    library.journals["bundle"] = [
        SimpleNamespace(ordinal=0, source_fingerprint=original_fingerprint)
    ]
    store = _store(tmp_path)
    attempt = await _attempt(
        store,
        root,
        source="soulseek",
        paths=[source],
        bundle_ids=["bundle"],
    )
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            file_paths=[str(source)],
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, library, lambda source: client, lambda: root
    )

    await service.cleanup_now(attempt.id, worker_id="first")

    attention = await store.get_download_attempt(attempt.id)
    assert attention.state == "needs_attention"
    assert attention.error_code == "source_file_fingerprint_changed"
    assert attention.materialized_fingerprints == {
        str(source.resolve()): original_fingerprint
    }
    assert source.read_bytes() == b"unrelated replacement"
    assert client.discarded == 0


@pytest.mark.asyncio
async def test_publisher_cleanup_pending_defers_without_counting_a_failure(
    tmp_path: Path,
):
    root = tmp_path / "sab"
    workspace = root / f"droppedneedle-{'a' * 32}-0"
    workspace.mkdir(parents=True)
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace, bundle_ids=["bundle"])
    library = _LibraryStore()
    library.bundles["bundle"] = "cleanup_pending"
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, library, lambda source: client, lambda: root
    )

    await service.cleanup_now(attempt.id, worker_id="test")

    deferred = await store.get_download_attempt(attempt.id)
    assert deferred.state == "cleanup_pending"
    assert deferred.cleanup_failures == 0
    assert deferred.error_code == "publisher_cleanup_pending"
    assert workspace.exists()
    assert client.discarded == 0


@pytest.mark.asyncio
async def test_repaired_publisher_attention_returns_to_cleanup(tmp_path: Path):
    now = [10.0]
    root = tmp_path / "sab"
    workspace = root / f"droppedneedle-{'a' * 32}-0"
    workspace.mkdir(parents=True)
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace, bundle_ids=["bundle"])
    library = _LibraryStore()
    library.bundles["bundle"] = "needs_attention"
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, library, lambda source: client, lambda: root, clock=lambda: now[0]
    )
    await service.cleanup_now(attempt.id, worker_id="blocked")
    attention = await store.get_download_attempt(attempt.id)
    assert attention.state == "needs_attention"

    library.bundles["bundle"] = "completed"
    now[0] = attention.next_retry_at
    await service.run_once("barrier-repaired")
    pending = await store.get_download_attempt(attempt.id)
    assert pending.state == "cleanup_pending"

    await service.run_once("cleanup")
    assert (await store.get_download_attempt(attempt.id)).state == "complete"
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_health_warning_starts_after_three_failures_and_auto_heals(
    tmp_path: Path,
):
    now = [10.0]
    root = tmp_path / "sab"
    workspace = root / f"droppedneedle-{'a' * 32}-0"
    workspace.mkdir(parents=True)
    (workspace / "track.flac").write_bytes(b"source")
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=False,
        )
    )
    service = AcquisitionCleanupService(
        store,
        _LibraryStore(),
        lambda source: client,
        lambda: root,
        clock=lambda: now[0],
    )

    for index in range(3):
        await service.cleanup_now(attempt.id, worker_id=f"failure-{index}")
        current = await store.get_download_attempt(attempt.id)
        now[0] = current.next_retry_at

    assert service_health.is_degraded("acquisition_cleanup", "source files")
    (entry,) = [
        item
        for item in service_health.current()
        if item.service == "acquisition_cleanup"
    ]
    assert entry.message == (
        "Temporary files couldn't be removed for 1 download. "
        "Your library is safe. Retrying automatically."
    )

    client.materialization.mount_healthy = True
    await service.cleanup_now(attempt.id, worker_id="recovered")

    assert (await store.get_download_attempt(attempt.id)).state == "complete"
    assert not service_health.is_degraded("acquisition_cleanup", "source files")


@pytest.mark.asyncio
async def test_attention_debt_heals_after_unsafe_workspace_is_removed(tmp_path: Path):
    now = [10.0]
    root = tmp_path / "sab"
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = root / f"droppedneedle-{'a' * 32}-0"
    root.mkdir()
    workspace.symlink_to(outside, target_is_directory=True)
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store,
        _LibraryStore(),
        lambda source: client,
        lambda: root,
        clock=lambda: now[0],
    )
    await service.cleanup_now(attempt.id, worker_id="unsafe")
    attention = await store.get_download_attempt(attempt.id)
    assert attention.state == "needs_attention"

    workspace.unlink()
    now[0] = attention.next_retry_at
    await service.run_once("recheck")

    assert (await store.get_download_attempt(attempt.id)).state == "complete"
    assert not service_health.is_degraded("acquisition_cleanup", "source files")


@pytest.mark.asyncio
async def test_read_only_workspace_retries_without_deleting_source(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root can unlink from a read-only test directory")
    root = tmp_path / "sab"
    workspace = root / f"droppedneedle-{'a' * 32}-0"
    workspace.mkdir(parents=True)
    source = workspace / "track.flac"
    source.write_bytes(b"keep")
    workspace.chmod(0o500)
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=workspace)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            workspace_path=str(workspace),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )
    try:
        await service.cleanup_now(attempt.id, worker_id="test")
    finally:
        workspace.chmod(0o700)
    assert source.exists()
    assert (await store.get_download_attempt(attempt.id)).state == "cleanup_pending"


@pytest.mark.asyncio
async def test_legacy_reconciliation_cleans_only_unambiguous_terminal_tasks(
    tmp_path: Path,
):
    root = tmp_path / "sab"
    category = root / "audio"
    category.mkdir(parents=True)
    store = _store(tmp_path)

    cleanable = []
    for index, status in enumerate(("completed", "partial", "cancelled"), start=1):
        task = await store.create_task(
            user_id="user-a",
            release_group_mbid=f"rg-{index}",
            artist_name="A",
            album_title=status,
        )
        fields = (
            {"cancelled_at": 10.0} if status == "cancelled" else {"completed_at": 10.0}
        )
        await store.update_status(task.id, status, **fields)
        cleanable.append(task)
    failed = await store.create_task(
        user_id="user-a",
        release_group_mbid="rg-4",
        artist_name="A",
        album_title="Failed",
    )
    active = await store.create_task(
        user_id="user-a",
        release_group_mbid="rg-5",
        artist_name="A",
        album_title="Active",
    )
    publisher_attention = await store.create_task(
        user_id="user-a", release_group_mbid="rg-6", artist_name="A", album_title="Held"
    )
    await store.update_status(failed.id, "failed", completed_at=10.0)
    await store.update_status(active.id, "downloading", started_at=10.0)
    await store.update_status(publisher_attention.id, "completed", completed_at=10.0)

    cleanable_workspaces = [
        category / f"droppedneedle-{task.id}-0" for task in cleanable
    ]
    failed_workspace = category / f"droppedneedle-{failed.id}-0"
    active_workspace = category / f"droppedneedle-{active.id}-0"
    attention_workspace = category / f"droppedneedle-{publisher_attention.id}-0"
    unknown_workspace = category / f"droppedneedle-{'f' * 32}-0"
    for workspace in (
        *cleanable_workspaces,
        failed_workspace,
        active_workspace,
        attention_workspace,
        unknown_workspace,
    ):
        workspace.mkdir()
        (workspace / "keep.flac").write_bytes(b"x")
    symlink_name = f"droppedneedle-{'e' * 32}-0"
    (category / symlink_name).symlink_to(unknown_workspace, target_is_directory=True)

    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    library = _LibraryStore()
    library.task_bundles[publisher_attention.id] = [
        SimpleNamespace(id="attention-bundle", state="needs_attention")
    ]
    library.bundles["attention-bundle"] = "needs_attention"
    service = AcquisitionCleanupService(
        store,
        library,
        lambda source: client,
        lambda: root,
        sab_category_getter=lambda: "audio",
    )

    await service.recover_startup()
    await service.reconcile_legacy_mount(limit=2)

    assert all(not workspace.exists() for workspace in cleanable_workspaces)
    assert failed_workspace.exists()
    assert active_workspace.exists()
    assert attention_workspace.exists()
    assert unknown_workspace.exists()
    assert (
        await store.get_download_attempt_for_job(
            "usenet", f"droppedneedle-{failed.id}-0"
        )
    ).state == "needs_attention"
    assert (
        await store.get_download_attempt_for_job(
            "usenet", f"droppedneedle-{active.id}-0"
        )
    ).state == "in_use"
    assert (
        await store.get_download_attempt_for_job(
            "usenet", f"droppedneedle-{publisher_attention.id}-0"
        )
    ).state == "needs_attention"
    assert (
        await store.get_download_attempt_for_job(
            "usenet", f"droppedneedle-{'f' * 32}-0"
        )
    ).state == "needs_attention"
    assert (
        await store.get_download_attempt_for_job("usenet", symlink_name)
    ).state == "needs_attention"
    assert service_health.is_degraded("acquisition_cleanup", "source files")


@pytest.mark.asyncio
async def test_reconciliation_read_error_preserves_durable_cursor(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "sab"
    (root / "category").mkdir(parents=True)
    store = _store(tmp_path)
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )
    original_entries = cleanup_module._directory_entries

    def fail_read(path: Path):
        raise OSError("temporary read failure")

    monkeypatch.setattr(cleanup_module, "_directory_entries", fail_read)
    with pytest.raises(OSError, match="temporary read failure"):
        await service.reconcile_legacy_mount()

    mount_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    progress = await store.ensure_cleanup_reconciliation(mount_key, str(root))
    assert progress.pending_directories == ["."]
    assert progress.current_directory is None
    assert progress.completed is False

    monkeypatch.setattr(cleanup_module, "_directory_entries", original_entries)
    assert await service.reconcile_legacy_mount() > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mount", [Path("."), Path("/")])
async def test_reconciliation_refuses_unsafe_mounts(tmp_path: Path, mount: Path):
    store = _store(tmp_path)
    client = _Client(DownloadMaterialization(state="missing"))
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: mount
    )

    assert await service.reconcile_legacy_mount() == 0


@pytest.mark.asyncio
async def test_reconciliation_survives_directory_removed_between_passes(
    tmp_path: Path,
):
    root = tmp_path / "sab"
    vanished = root / "droppedneedle-staging"
    vanished.mkdir(parents=True)
    store = _store(tmp_path)
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )

    assert await service.reconcile_legacy_mount(limit=1) == 1
    vanished.rmdir()

    await service.reconcile_legacy_mount()

    mount_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    progress = await store.ensure_cleanup_reconciliation(mount_key, str(root))
    assert progress.completed is True


def test_directory_entries_skips_entry_removed_mid_scan(tmp_path: Path, monkeypatch):
    class _VanishingEntry:
        def __init__(self, name: str) -> None:
            self.name = name
            self.path = str(tmp_path / name)

        def is_dir(self, follow_symlinks: bool = False) -> bool:
            raise FileNotFoundError(self.name)

        def is_symlink(self) -> bool:
            return False

    class _FakeEntries:
        def __enter__(self):
            return iter([_VanishingEntry("gone")])

        def __exit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr(cleanup_module.os, "scandir", lambda path: _FakeEntries())
    assert cleanup_module._directory_entries(tmp_path) == []


def test_directory_entries_returns_empty_for_vanished_directory(tmp_path: Path):
    assert cleanup_module._directory_entries(tmp_path / "missing") == []


@pytest.mark.asyncio
async def test_reconciliation_default_category_scopes_to_droppedneedle_prefix(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "sab"
    (root / "droppedneedle-staging").mkdir(parents=True)
    foreign = root / "sonarr"
    foreign_job = foreign / f"droppedneedle-{'f' * 32}-0"
    foreign_job.mkdir(parents=True)
    store = _store(tmp_path)
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )
    visited: list[str] = []
    original_entries = cleanup_module._directory_entries

    def recording_entries(path: Path):
        visited.append(str(path))
        return original_entries(path)

    monkeypatch.setattr(cleanup_module, "_directory_entries", recording_entries)

    await service.reconcile_legacy_mount()

    assert str(root) in visited
    assert str(root / "droppedneedle-staging") in visited
    assert not any(str(foreign) in path for path in visited)
    assert (
        await store.get_download_attempt_for_job(
            "usenet", f"droppedneedle-{'f' * 32}-0"
        )
        is None
    )


@pytest.mark.asyncio
async def test_reconciliation_configured_category_descends_only_there(
    tmp_path: Path,
):
    root = tmp_path / "sab"
    category_job = root / "audio" / f"droppedneedle-{'f' * 32}-0"
    category_job.mkdir(parents=True)
    foreign_job = root / "movies" / f"droppedneedle-{'e' * 32}-0"
    foreign_job.mkdir(parents=True)
    store = _store(tmp_path)
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = AcquisitionCleanupService(
        store,
        _LibraryStore(),
        lambda source: client,
        lambda: root,
        sab_category_getter=lambda: "Audio",
    )

    await service.reconcile_legacy_mount()

    assert (
        await store.get_download_attempt_for_job(
            "usenet", f"droppedneedle-{'f' * 32}-0"
        )
    ).state == "needs_attention"
    assert (
        await store.get_download_attempt_for_job(
            "usenet", f"droppedneedle-{'e' * 32}-0"
        )
        is None
    )


def _age_folder(path: Path) -> None:
    stale = 1_000_000_000.0  # 2001 - always older than ORPHAN_MIN_AGE_SECONDS
    os.utime(path, (stale, stale))


def _orphan_service(
    tmp_path: Path,
    root: Path,
    store: DownloadStore,
    client: _Client,
    library: _LibraryStore | None = None,
) -> AcquisitionCleanupService:
    return AcquisitionCleanupService(
        store,
        library or _LibraryStore(),
        lambda source: client,
        lambda: root,
    )


@pytest.mark.asyncio
async def test_orphan_reconcile_removes_unowned_stale_dn_folder(tmp_path: Path):
    root = tmp_path / "sab"
    workspace = root / f"droppedneedle-{'a' * 32}-0"
    workspace.mkdir(parents=True)
    (workspace / "album.flac").write_bytes(b"x")
    _age_folder(workspace)
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = _orphan_service(tmp_path, root, _store(tmp_path), client)

    assert await service.reconcile_orphan_folders() == 1
    assert not workspace.exists()
    assert client.discarded == 1


@pytest.mark.asyncio
async def test_orphan_reconcile_leaves_folder_owned_by_attention_journal(
    tmp_path: Path,
):
    root = tmp_path / "sab"
    task_id = "b" * 32
    job_name = f"droppedneedle-{task_id}-0"
    workspace = root / job_name
    workspace.mkdir(parents=True)
    _age_folder(workspace)
    store = _store(tmp_path)
    attempt = await store.create_download_attempt(
        task_id=task_id,
        source="usenet",
        candidate_index=0,
        job_name=job_name,
        handle=TaskHandle(source="usenet", job_name=job_name),
        now=1.0,
    )
    await store.transition_download_attempt(
        attempt.id,
        expected_row_revision=attempt.row_revision,
        new_state="needs_attention",
        now=2.0,
    )
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = _orphan_service(tmp_path, root, store, client)

    assert await service.reconcile_orphan_folders() == 0
    assert workspace.exists()
    assert client.discarded == 0


@pytest.mark.asyncio
async def test_orphan_reconcile_leaves_active_task_workspace_without_journal_row(
    tmp_path: Path,
):
    root = tmp_path / "sab"
    store = _store(tmp_path)
    active = await store.create_task(
        user_id="user-a",
        release_group_mbid="rg-1",
        artist_name="A",
        album_title="Active",
    )
    await store.update_status(active.id, "downloading", started_at=10.0)
    workspace = root / f"droppedneedle-{active.id}-0"
    workspace.mkdir(parents=True)
    _age_folder(workspace)
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = _orphan_service(tmp_path, root, store, client)

    assert await service.reconcile_orphan_folders() == 0
    assert workspace.exists()


@pytest.mark.asyncio
async def test_orphan_reconcile_leaves_young_folder_alone(tmp_path: Path):
    root = tmp_path / "sab"
    workspace = root / f"droppedneedle-{'c' * 32}-0"
    workspace.mkdir(parents=True)  # fresh mtime, below the age floor
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = _orphan_service(tmp_path, root, _store(tmp_path), client)

    assert await service.reconcile_orphan_folders() == 0
    assert workspace.exists()
    assert client.discarded == 0


@pytest.mark.asyncio
async def test_orphan_reconcile_never_touches_foreign_folder_names(tmp_path: Path):
    root = tmp_path / "sab"
    foreign = root / "sonarr.something"
    foreign.mkdir(parents=True)
    (foreign / "episode.mkv").write_bytes(b"x")
    _age_folder(foreign)
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = _orphan_service(tmp_path, root, _store(tmp_path), client)

    assert await service.reconcile_orphan_folders() == 0
    assert foreign.exists()


@pytest.mark.asyncio
async def test_orphan_reconcile_handles_sab_collision_suffixes(tmp_path: Path):
    root = tmp_path / "sab"
    owned_task = "d" * 32
    owned_job = f"droppedneedle-{owned_task}-0"
    orphan_suffixed = root / f"droppedneedle-{'c' * 32}-0.1"
    kept_suffixed = root / f"{owned_job}.2"
    orphan_suffixed.mkdir(parents=True)
    kept_suffixed.mkdir(parents=True)
    _age_folder(orphan_suffixed)
    _age_folder(kept_suffixed)
    store = _store(tmp_path)
    attempt = await store.create_download_attempt(
        task_id=owned_task,
        source="usenet",
        candidate_index=0,
        job_name=owned_job,
        handle=TaskHandle(source="usenet", job_name=owned_job),
        now=1.0,
    )
    await store.transition_download_attempt(
        attempt.id,
        expected_row_revision=attempt.row_revision,
        new_state="needs_attention",
        now=2.0,
    )
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = _orphan_service(tmp_path, root, store, client)

    assert await service.reconcile_orphan_folders() == 1
    assert not orphan_suffixed.exists()
    assert kept_suffixed.exists()


@pytest.mark.asyncio
async def test_orphan_reconcile_honours_publisher_barrier(tmp_path: Path):
    root = tmp_path / "sab"
    held_task = "e" * 32
    workspace = root / f"droppedneedle-{held_task}-0"
    workspace.mkdir(parents=True)
    _age_folder(workspace)
    library = _LibraryStore()
    library.task_bundles[held_task] = [
        SimpleNamespace(id="held-bundle", state="needs_attention")
    ]
    client = _Client(
        DownloadMaterialization(
            state="missing", mount_root=str(root), mount_healthy=True
        )
    )
    service = _orphan_service(
        tmp_path, root, _store(tmp_path), client, library=library
    )

    assert await service.reconcile_orphan_folders() == 0
    assert workspace.exists()
    assert client.discarded == 0


def _slskd_service(
    store: DownloadStore,
    library: _LibraryStore,
    mount: Path,
) -> AcquisitionCleanupService:
    return AcquisitionCleanupService(
        store,
        library,
        lambda source: _Client(
            DownloadMaterialization(state="missing", mount_healthy=True)
        ),
        lambda: mount / "unused-sab",
        slskd_mount_getter=lambda: mount,
    )


def _write_stale_track(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    _age_folder(path)
    return hashlib.sha256(content).hexdigest()


@pytest.mark.asyncio
async def test_slskd_reconcile_removes_fully_imported_folder(tmp_path: Path):
    mount = tmp_path / "slskd"
    folder = mount / "peer" / "Artist - Album"
    fa = _write_stale_track(folder / "01.flac", b"track-one")
    fb = _write_stale_track(folder / "02.flac", b"track-two")
    _age_folder(folder)
    _age_folder(mount / "peer")
    library = _LibraryStore()
    library.committed_fingerprints = {fa, fb}
    service = _slskd_service(_store(tmp_path), library, mount)

    result = await service.reconcile_slskd_orphans()

    assert result.removed == [str(mount / "peer")]
    assert result.kept == []
    assert not folder.exists()
    assert not (mount / "peer").exists()
    assert mount.exists()  # the mount itself is never removed


@pytest.mark.asyncio
async def test_slskd_reconcile_keeps_folder_with_unimported_track(tmp_path: Path):
    mount = tmp_path / "slskd"
    folder = mount / "Artist - Album"
    fa = _write_stale_track(folder / "01.flac", b"imported")
    _write_stale_track(folder / "02.flac", b"never-imported")
    _age_folder(folder)
    library = _LibraryStore()
    library.committed_fingerprints = {fa}  # second track absent from the library
    service = _slskd_service(_store(tmp_path), library, mount)

    result = await service.reconcile_slskd_orphans()

    assert result.removed == []
    assert result.kept == [(str(folder), "unimported_audio")]
    assert folder.exists()
    assert (folder / "01.flac").exists()


@pytest.mark.asyncio
async def test_slskd_reconcile_keeps_recently_modified_folder(tmp_path: Path):
    mount = tmp_path / "slskd"
    folder = mount / "Artist - Album"
    folder.mkdir(parents=True)
    content = b"fresh-download"
    (folder / "01.flac").write_bytes(content)  # fresh mtime, below the age floor
    library = _LibraryStore()
    library.committed_fingerprints = {hashlib.sha256(content).hexdigest()}
    service = _slskd_service(_store(tmp_path), library, mount)

    result = await service.reconcile_slskd_orphans()

    assert result.removed == []
    assert result.kept == [(str(folder), "recently_modified")]
    assert folder.exists()


@pytest.mark.asyncio
async def test_slskd_reconcile_dry_run_reports_without_deleting(tmp_path: Path):
    mount = tmp_path / "slskd"
    folder = mount / "Artist - Album"
    fa = _write_stale_track(folder / "01.flac", b"only-track")
    _age_folder(folder)
    library = _LibraryStore()
    library.committed_fingerprints = {fa}
    service = _slskd_service(_store(tmp_path), library, mount)

    result = await service.reconcile_slskd_orphans(dry_run=True)

    assert result.dry_run is True
    assert result.removed == [str(folder)]
    assert folder.exists()  # dry run never touches disk
    assert (folder / "01.flac").exists()


@pytest.mark.asyncio
async def test_slskd_reconcile_removes_imported_album_but_keeps_sibling(tmp_path: Path):
    mount = tmp_path / "slskd"
    peer = mount / "peer"
    imported = peer / "Imported Album"
    unimported = peer / "Unimported Album"
    fa = _write_stale_track(imported / "01.flac", b"imported-track")
    _write_stale_track(unimported / "01.flac", b"unimported-track")
    for path in (imported, unimported, peer):
        _age_folder(path)
    library = _LibraryStore()
    library.committed_fingerprints = {fa}
    service = _slskd_service(_store(tmp_path), library, mount)

    result = await service.reconcile_slskd_orphans()

    assert result.removed == [str(imported)]
    assert not imported.exists()
    assert unimported.exists()  # the peer folder and its unimported album survive
    assert peer.exists()


@pytest.mark.asyncio
async def test_slskd_reconcile_noop_without_mount_getter(tmp_path: Path):
    mount = tmp_path / "slskd"
    (mount / "Album").mkdir(parents=True)
    service = AcquisitionCleanupService(
        _store(tmp_path),
        _LibraryStore(),
        lambda source: _Client(
            DownloadMaterialization(state="missing", mount_healthy=True)
        ),
        lambda: mount / "unused-sab",
    )

    result = await service.reconcile_slskd_orphans()

    assert result == ([], [], 0, False)


@pytest.mark.asyncio
async def test_slskd_reconcile_prunes_old_empty_folder(tmp_path: Path):
    mount = tmp_path / "slskd"
    empty = mount / "Leftover Skeleton"
    empty.mkdir(parents=True)
    _age_folder(empty)
    service = _slskd_service(_store(tmp_path), _LibraryStore(), mount)

    result = await service.reconcile_slskd_orphans()

    assert result.removed == [str(empty)]
    assert not empty.exists()
    assert mount.exists()


@pytest.mark.asyncio
async def test_slskd_reconcile_keeps_fresh_empty_folder(tmp_path: Path):
    mount = tmp_path / "slskd"
    empty = mount / "In Progress"
    empty.mkdir(parents=True)  # fresh mtime: could be a download about to land
    service = _slskd_service(_store(tmp_path), _LibraryStore(), mount)

    result = await service.reconcile_slskd_orphans()

    assert result.removed == []
    assert empty.exists()


@pytest.mark.asyncio
async def test_slskd_reconcile_keeps_folder_with_only_non_audio(tmp_path: Path):
    mount = tmp_path / "slskd"
    folder = mount / "Just Artwork"
    folder.mkdir(parents=True)
    (folder / "cover.jpg").write_bytes(b"art")
    _age_folder(folder / "cover.jpg")
    _age_folder(folder)
    service = _slskd_service(_store(tmp_path), _LibraryStore(), mount)

    result = await service.reconcile_slskd_orphans()

    assert result.removed == []
    assert folder.exists()


@pytest.mark.asyncio
async def test_slskd_reconcile_prunes_nested_empty_tree(tmp_path: Path):
    mount = tmp_path / "slskd"
    nested = mount / "Peer" / "Album" / "Disc 1"
    nested.mkdir(parents=True)
    for path in (nested, nested.parent, nested.parent.parent):
        _age_folder(path)
    service = _slskd_service(_store(tmp_path), _LibraryStore(), mount)

    result = await service.reconcile_slskd_orphans()

    # The whole empty branch collapses to a single top-level removal.
    assert result.removed == [str(mount / "Peer")]
    assert not (mount / "Peer").exists()


def _nested_release_workspace(root: Path) -> tuple[Path, Path]:
    """SABnzbd unpacks a release that carries its own top folder one level down."""
    job = root / f"droppedneedle-{'a' * 32}-0"
    nested = job / "2008 - The Fame"
    nested.mkdir(parents=True)
    (nested / "01 - Just Dance.flac").write_bytes(b"source")
    return job, nested


@pytest.mark.asyncio
async def test_nested_release_folder_removes_the_whole_job_directory(tmp_path: Path):
    root = tmp_path / "sab"
    job, nested = _nested_release_workspace(root)
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=nested)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            workspace_path=str(nested),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )

    assert await service.cleanup_now(attempt.id, worker_id="test") is True

    assert not job.exists()
    assert root.exists()
    assert (await store.get_download_attempt(attempt.id)).state == "complete"


@pytest.mark.asyncio
async def test_workspace_without_a_job_directory_is_still_refused(tmp_path: Path):
    root = tmp_path / "sab"
    foreign = root / "someone else" / "2008 - The Fame"
    foreign.mkdir(parents=True)
    (foreign / "keep.flac").write_bytes(b"keep")
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=foreign)
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            workspace_path=str(foreign),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root
    )

    await service.cleanup_now(attempt.id, worker_id="test")

    parked = await store.get_download_attempt(attempt.id)
    assert parked.state == "needs_attention"
    assert parked.error_code == "workspace_identity_conflict"
    assert (foreign / "keep.flac").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code", ["workspace_identity_conflict", "workspace_evidence_missing"]
)
async def test_attention_parked_by_an_old_identity_check_returns_to_cleanup(
    tmp_path: Path, error_code: str
):
    """Rows the exact-name check parked stayed needs_attention forever and blocked
    the orphan reconciler too; the recheck now requeues them once the job directory
    resolves."""
    now = [10.0]
    root = tmp_path / "sab"
    job, nested = _nested_release_workspace(root)
    store = _store(tmp_path)
    attempt = await _attempt(store, root, workspace=nested)
    parked = await store.transition_download_attempt(
        attempt.id,
        expected_row_revision=attempt.row_revision,
        new_state="needs_attention",
        now=5.0,
        disposition="preserve",
        error_code=error_code,
        mount_root=str(root),
        workspace_path=str(nested),
        next_retry_at=5.0,
    )
    assert parked is not None
    client = _Client(
        DownloadMaterialization(
            state="completed",
            mount_root=str(root),
            workspace_path=str(nested),
            mount_healthy=True,
        )
    )
    service = AcquisitionCleanupService(
        store, _LibraryStore(), lambda source: client, lambda: root, clock=lambda: now[0]
    )

    await service.run_once("recheck")
    requeued = await store.get_download_attempt(attempt.id)
    assert requeued.state == "cleanup_pending"
    assert requeued.disposition == "discard"

    await service.run_once("cleanup")
    assert (await store.get_download_attempt(attempt.id)).state == "complete"
    assert not job.exists()


@pytest.mark.asyncio
async def test_orphan_reconcile_takes_folders_parked_for_forgotten_sab_jobs(
    tmp_path: Path,
):
    """Once SABnzbd forgets a job the cleanup worker refuses to delete on the journal's
    word alone (fresh_workspace_evidence_missing) and no user action resolves that, so
    the row pinned the folder forever. The orphan reconciler's own gates decide it,
    and the journal row is closed so the health warning clears with the folder."""
    root = tmp_path / "sab"
    task_id = "c" * 32
    job_name = f"droppedneedle-{task_id}-0"
    workspace = root / job_name
    workspace.mkdir(parents=True)
    (workspace / "album.flac").write_bytes(b"x")
    _age_folder(workspace)
    store = _store(tmp_path)
    attempt = await store.create_download_attempt(
        task_id=task_id,
        source="usenet",
        candidate_index=0,
        job_name=job_name,
        handle=TaskHandle(source="usenet", job_name=job_name),
        now=1.0,
    )
    await store.transition_download_attempt(
        attempt.id,
        expected_row_revision=attempt.row_revision,
        new_state="needs_attention",
        now=2.0,
        error_code="fresh_workspace_evidence_missing",
    )
    client = _Client(
        DownloadMaterialization(state="missing", mount_root=str(root), mount_healthy=True)
    )
    service = _orphan_service(tmp_path, root, store, client)

    assert await service.reconcile_orphan_folders() == 1
    assert not workspace.exists()
    closed = await store.get_download_attempt(attempt.id)
    assert (closed.state, closed.error_code) == ("complete", None)
