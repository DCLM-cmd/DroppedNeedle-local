"""D-EDITION-AUTO S-3 settings schema + store ratchet coverage."""

import sqlite3
import threading

import pytest

from api.v1.schemas.library_management import (
    IdentityManagementSettings,
    LibraryManagementProfile,
    LibraryManagementRootAssignment,
    LibraryManagementRootOverrides,
    LibraryManagementSettings,
    profile_revision,
    settings_revision,
)
from infrastructure.persistence.native_library_store import NativeLibraryStore
from services.native.library_management_profile_service import (
    LibraryManagementProfileService,
)


def test_identity_section_defaults_off() -> None:
    profile = LibraryManagementProfile(id="p", name="P")
    assert profile.identity.automatic_edition_acceptance_enabled is False


def test_default_identity_section_keeps_profile_revision_stable() -> None:
    """The additive identity section must not shift stored revisions while
    it sits at its default (the strip helper removes it before hashing)."""
    enabled = LibraryManagementProfile(
        id="p",
        name="P",
        identity=IdentityManagementSettings(
            automatic_edition_acceptance_enabled=True
        ),
    )
    disabled = LibraryManagementProfile(
        id="p",
        name="P",
        identity=IdentityManagementSettings(
            automatic_edition_acceptance_enabled=False
        ),
    )
    legacy_equivalent = LibraryManagementProfile(id="p", name="P")
    assert profile_revision(disabled) == profile_revision(legacy_equivalent)
    assert profile_revision(enabled) != profile_revision(legacy_equivalent)

    settings = LibraryManagementSettings(profiles=[disabled])
    stripped = LibraryManagementSettings(profiles=[legacy_equivalent])
    assert settings_revision(settings) == settings_revision(stripped)


def test_root_override_wins_over_profile_flag() -> None:
    service = LibraryManagementProfileService.__new__(
        LibraryManagementProfileService
    )
    settings = LibraryManagementSettings(
        profiles=[
            LibraryManagementProfile(
                id="p",
                name="P",
                identity=IdentityManagementSettings(
                    automatic_edition_acceptance_enabled=True
                ),
            )
        ],
        default_profile_id="p",
        root_assignments=[
            LibraryManagementRootAssignment(root_id="root-a"),
            LibraryManagementRootAssignment(
                root_id="root-b",
                overrides=LibraryManagementRootOverrides(
                    automatic_edition_acceptance_enabled=False
                ),
            ),
            LibraryManagementRootAssignment(
                root_id="root-c",
                overrides=LibraryManagementRootOverrides(
                    automatic_edition_acceptance_enabled=True
                ),
            ),
        ],
    )
    effective_a = LibraryManagementProfileService._effective_profile(
        settings, settings.root_assignments[0]
    )
    effective_b = LibraryManagementProfileService._effective_profile(
        settings, settings.root_assignments[1]
    )
    effective_c = LibraryManagementProfileService._effective_profile(
        settings, settings.root_assignments[2]
    )
    # Inherit from the assigned profile...
    assert effective_a.identity.automatic_edition_acceptance_enabled is True
    # ...and a per-root override wins when present.
    assert effective_b.identity.automatic_edition_acceptance_enabled is False
    assert effective_c.identity.automatic_edition_acceptance_enabled is True


@pytest.mark.asyncio
async def test_automatic_edition_undo_table_construct_twice_idempotent(
    tmp_path,
) -> None:
    path = tmp_path / "library.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    first = NativeLibraryStore(path, threading.Lock())
    second = NativeLibraryStore(path, threading.Lock())
    rows = await second._read(
        lambda connection_: connection_
        .execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'library_automatic_edition_undo'"
        )
        .fetchall()
    )
    assert len(rows) == 1
    del first
