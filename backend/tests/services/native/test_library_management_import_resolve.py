"""Service slice of the admin resolve action for stuck import bundles."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.exceptions import ConflictError, ResourceNotFoundError
from models.library_management import (
    LibraryManagementImportBundleRecord,
    LibraryManagementImportJournal,
)
from services.native.library_management_publisher import LibraryManagementPublisher
from services.native.library_management_recovery_service import (
    LibraryManagementRecoveryService,
)
from tests.services.native.test_library_management_publisher import (
    _import_publication_fixture,
)


def _recovery(publisher, store) -> LibraryManagementRecoveryService:  # noqa: ANN001, ANN202
    return LibraryManagementRecoveryService(
        store,
        publisher,
        publisher._filesystem,
        clock=lambda: 120.0,
    )


async def _stick_bundle(
    store,  # noqa: ANN001
    root: Path,
    policy_revision: str,
    *,
    key: str,
    files: dict[int, bytes | None],
    staged: dict[int, str | None] | None = None,
    destination_root_id: str = "root-1",
) -> str:
    """Seed a `needs_attention` bundle; None content means a missing file."""
    bundle_id = f"bundle-{key}"
    await store.ensure_library_management_import_bundle(
        LibraryManagementImportBundleRecord(
            id=bundle_id,
            idempotency_key=f"key-{key}",
            origin="acquisition",
            policy_revision=policy_revision,
            request_json="{}",
            request_hash="00" * 32,
            state="preparing",
            created_at=1.0,
            updated_at=1.0,
        )
    )
    staged = staged or {}
    for ordinal, content in files.items():
        relative = f"Import Artist/Import Album/{ordinal:02d} Seeded.flac"
        if content is not None:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            fingerprint = hashlib.sha256(content).hexdigest()
        else:
            fingerprint = hashlib.sha256(b"absent").hexdigest()
        await store.ensure_library_management_import_journal(
            LibraryManagementImportJournal(
                bundle_id=bundle_id,
                ordinal=ordinal,
                state="planned",
                source_fingerprint="b" * 64,
                source_size=len(content) if content is not None else 0,
                source_mtime_ns=5,
                temporary_relative_path=f"tmp-{ordinal}.flac",
                destination_root_id=destination_root_id,
                destination_relative_path=relative,
                staged_fingerprint=staged.get(ordinal, fingerprint),
                created_at=1.0,
                updated_at=1.0,
            )
        )
    await store.mark_library_management_import_needs_attention(
        bundle_id, failure_code="SEED", updated_at=2.0
    )
    return bundle_id


async def _states(store, bundle_id: str):  # noqa: ANN001, ANN202
    record = await store.get_library_management_import_bundle(bundle_id)
    journals = await store.list_library_management_import_journals(bundle_id)
    assert record is not None
    return record.state, {journal.ordinal: journal.state for journal in journals}


@pytest.mark.asyncio
async def test_resolve_verifies_and_flips_bundle_and_journals(
    tmp_path: Path,
) -> None:
    root, _source, store, _audio, publisher, _service, policy_revision = (
        _import_publication_fixture(tmp_path)
    )
    content = b"verified-library-bytes"
    bundle_id = await _stick_bundle(
        store, root, policy_revision, key="ok", files={0: content, 1: content}
    )
    sentinel = root / "tmp-keep.flac"
    sentinel.write_bytes(b"user staging data")

    result = await _recovery(publisher, store).resolve_import_bundle(bundle_id)

    assert result == {
        "bundle_id": bundle_id,
        "state": "resolved",
        "verified_files": 2,
        "total_files": 2,
    }
    state, journals = await _states(store, bundle_id)
    assert state == "resolved"
    assert journals == {0: "resolved", 1: "resolved"}
    # Temporaries are user data: resolve never deletes them.
    assert sentinel.read_bytes() == b"user staging data"


@pytest.mark.asyncio
async def test_resolve_fingerprint_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    root, _source, store, _audio, publisher, _service, policy_revision = (
        _import_publication_fixture(tmp_path)
    )
    bundle_id = await _stick_bundle(
        store,
        root,
        policy_revision,
        key="mismatch",
        files={0: b"on-disk-bytes", 1: b"verified-library-bytes"},
        staged={0: hashlib.sha256(b"staged-bytes").hexdigest()},
    )
    before = await _states(store, bundle_id)

    with pytest.raises(ConflictError) as error:
        await _recovery(publisher, store).resolve_import_bundle(bundle_id)

    assert error.value.details == [{"ordinal": 0, "reason": "fingerprint_mismatch"}]
    assert await _states(store, bundle_id) == before


@pytest.mark.asyncio
async def test_resolve_missing_destination_fails_closed(
    tmp_path: Path,
) -> None:
    root, _source, store, _audio, publisher, _service, policy_revision = (
        _import_publication_fixture(tmp_path)
    )
    bundle_id = await _stick_bundle(
        store, root, policy_revision, key="missing", files={0: None}
    )
    before = await _states(store, bundle_id)

    with pytest.raises(ConflictError) as error:
        await _recovery(publisher, store).resolve_import_bundle(bundle_id)

    assert error.value.details == [{"ordinal": 0, "reason": "missing"}]
    assert await _states(store, bundle_id) == before


@pytest.mark.asyncio
async def test_resolve_null_staged_fingerprint_is_unverifiable(
    tmp_path: Path,
) -> None:
    root, _source, store, _audio, publisher, _service, policy_revision = (
        _import_publication_fixture(tmp_path)
    )
    bundle_id = await _stick_bundle(
        store,
        root,
        policy_revision,
        key="null-staged",
        files={0: b"on-disk-bytes"},
        staged={0: None},
    )
    before = await _states(store, bundle_id)

    with pytest.raises(ConflictError) as error:
        await _recovery(publisher, store).resolve_import_bundle(bundle_id)

    assert error.value.details == [{"ordinal": 0, "reason": "unverifiable"}]
    assert await _states(store, bundle_id) == before


@pytest.mark.asyncio
async def test_resolve_unknown_destination_root_is_unverifiable(
    tmp_path: Path,
) -> None:
    root, _source, store, _audio, publisher, _service, policy_revision = (
        _import_publication_fixture(tmp_path)
    )
    bundle_id = await _stick_bundle(
        store,
        root,
        policy_revision,
        key="bad-root",
        files={0: b"on-disk-bytes"},
        destination_root_id="root-gone",
    )
    before = await _states(store, bundle_id)

    with pytest.raises(ConflictError) as error:
        await _recovery(publisher, store).resolve_import_bundle(bundle_id)

    assert error.value.details == [{"ordinal": 0, "reason": "unverifiable"}]
    assert await _states(store, bundle_id) == before


@pytest.mark.asyncio
async def test_resolve_uses_publisher_hash_algorithm(tmp_path: Path) -> None:
    root, _source, store, _audio, publisher, _service, policy_revision = (
        _import_publication_fixture(tmp_path)
    )
    content = b"hash-parity-bytes"
    bundle_id = await _stick_bundle(
        store, root, policy_revision, key="parity", files={0: content}
    )
    journals = await store.list_library_management_import_journals(bundle_id)

    assert journals[0].staged_fingerprint == LibraryManagementPublisher._hash_file(
        root / journals[0].destination_relative_path
    )
    result = await _recovery(publisher, store).resolve_import_bundle(bundle_id)
    assert result["state"] == "resolved"


@pytest.mark.asyncio
async def test_resolve_wrong_state_raises_conflict(tmp_path: Path) -> None:
    root, _source, store, _audio, publisher, _service, policy_revision = (
        _import_publication_fixture(tmp_path)
    )
    bundle_id = f"bundle-preparing-{tmp_path.name}"
    await store.ensure_library_management_import_bundle(
        LibraryManagementImportBundleRecord(
            id=bundle_id,
            idempotency_key=f"key-preparing-{tmp_path.name}",
            origin="acquisition",
            policy_revision=policy_revision,
            request_json="{}",
            request_hash="00" * 32,
            state="preparing",
            created_at=1.0,
            updated_at=1.0,
        )
    )

    with pytest.raises(ConflictError):
        await _recovery(publisher, store).resolve_import_bundle(bundle_id)


@pytest.mark.asyncio
async def test_resolve_missing_bundle_raises_not_found(tmp_path: Path) -> None:
    root, _source, store, _audio, publisher, _service, _policy_revision = (
        _import_publication_fixture(tmp_path)
    )

    with pytest.raises(ResourceNotFoundError):
        await _recovery(publisher, store).resolve_import_bundle("bundle-missing")
