"""PreferencesService library settings: defaults, seeding, AcoustID mask/preserve."""

import json
from pathlib import Path

import pytest

from api.v1.schemas.library_policies import (
    LibraryPathPolicyRule,
    LibraryRootSettings,
    TypedLibrarySettings,
)
from api.v1.schemas.settings import (
    ACOUSTID_KEY_MASK,
    LibraryScanFilesystemWatcherSettings,
    LibraryScanScheduleSettings,
    LibrarySettings,
    LibrarySyncSettings,
)
from core.config import Settings
from core.exceptions import ConfigurationError
from services.preferences_service import PreferencesService


@pytest.fixture
def prefs(tmp_path: Path) -> PreferencesService:
    settings = Settings()
    settings.config_file_path = tmp_path / "config.json"
    return PreferencesService(settings)


def test_defaults_when_unset(prefs):
    settings = prefs.get_library_settings()
    assert settings.library_paths == ["/music"]
    assert settings.acoustid_api_key == ""  # no key configured yet


def test_scan_schedule_defaults_when_unset(prefs):
    assert prefs.get_library_scan_schedule().scan_frequency == "24hr"


def test_scan_schedule_migrates_from_legacy_sync_settings(prefs):
    prefs.save_library_sync_settings(LibrarySyncSettings(sync_frequency="6hr"))
    sched = prefs.get_library_scan_schedule()
    assert sched.scan_frequency == "6hr"  # carried over from the old Lidarr-era setting
    stored = json.loads(prefs._config_path.read_text())
    assert stored["library_scan_schedule"]["scan_frequency"] == "6hr"  # persisted under the new key


def test_acoustid_key_masked_on_read_decrypted_raw(prefs):
    prefs.save_library_settings(
        LibrarySettings(library_paths=["/m"], acoustid_api_key="secret-key")
    )
    assert prefs.get_library_settings().acoustid_api_key == ACOUSTID_KEY_MASK
    assert prefs.get_library_settings_raw().acoustid_api_key == "secret-key"


def test_acoustid_key_stored_encrypted(prefs):
    prefs.save_library_settings(
        LibrarySettings(library_paths=["/m"], acoustid_api_key="secret-key")
    )
    stored = json.loads(prefs._config_path.read_text())["library_settings"]["acoustid_api_key"]
    assert stored != "secret-key"  # ciphertext, not plaintext
    assert stored != ""


def test_mask_on_save_preserves_existing_key(prefs):
    prefs.save_library_settings(
        LibrarySettings(library_paths=["/m"], acoustid_api_key="secret-key")
    )
    # Re-save with the mask sentinel (as the UI would) + changed paths.
    prefs.save_library_settings(
        LibrarySettings(library_paths=["/m2"], acoustid_api_key=ACOUSTID_KEY_MASK)
    )
    raw = prefs.get_library_settings_raw()
    assert raw.acoustid_api_key == "secret-key"  # secret preserved
    assert raw.library_paths == ["/m2"]  # non-secret fields updated


def test_upgrade_retarget_rejects_unknown_root_ids(prefs):
    prefs.save_library_settings(LibrarySettings(library_paths=["/old/music"]))

    with pytest.raises(ConfigurationError, match="unknown library root"):
        prefs.retarget_library_roots_for_upgrade({"missing-root": "/elsewhere"})
    with pytest.raises(ConfigurationError, match="unknown library root"):
        prefs.retarget_library_roots_for_upgrade({})


def test_upgrade_retargets_only_existing_root_paths_and_preserves_secret(prefs):
    prefs.save_library_settings(
        LibrarySettings(library_paths=["/old/music"], acoustid_api_key="secret-key")
    )
    before = prefs.get_typed_library_settings()
    root = before.library_roots[0]
    prefs.save_typed_library_settings(
        TypedLibrarySettings(
            library_roots=[
                LibraryRootSettings(
                    id=root.id,
                    path=root.path,
                    label=root.label,
                    policy="local_metadata",
                    rules=[
                        LibraryPathPolicyRule(
                            id="rule", relative_path="Archive", policy="excluded"
                        )
                    ],
                )
            ],
            staging_path=before.staging_path,
            naming_template=before.naming_template,
            acoustid_api_key=before.acoustid_api_key,
        )
    )
    before = prefs.get_typed_library_settings()

    prefs.retarget_library_roots_for_upgrade(
        {before.library_roots[0].id: "/current/music"}
    )

    after = prefs.get_typed_library_settings()
    assert after.library_roots[0].id == before.library_roots[0].id
    assert after.library_roots[0].label == before.library_roots[0].label
    assert after.library_roots[0].path == "/current/music"
    assert after.library_roots[0].policy == "local_metadata"
    assert after.library_roots[0].rules == before.library_roots[0].rules
    assert prefs.get_typed_library_settings_raw().acoustid_api_key == "secret-key"


def test_normal_typed_save_still_rejects_moving_an_existing_root(prefs):
    current = prefs.get_typed_library_settings()
    root = current.library_roots[0]

    with pytest.raises(ConfigurationError, match="cannot be moved"):
        prefs.save_typed_library_settings(
            TypedLibrarySettings(
                library_roots=[
                    LibraryRootSettings(
                        id=root.id,
                        path="/different",
                        label=root.label,
                        policy=root.policy,
                        rules=root.rules,
                    )
                ],
                staging_path=current.staging_path,
                naming_template=current.naming_template,
                acoustid_api_key=current.acoustid_api_key,
            )
        )


def test_seeds_library_paths_from_legacy_root(tmp_path: Path):
    settings = Settings()
    settings.config_file_path = tmp_path / "config.json"
    settings.config_file_path.parent.mkdir(parents=True, exist_ok=True)
    settings.config_file_path.write_text(
        json.dumps({"_legacy_lidarr": {"root_folder_path": "/legacy/music"}})
    )
    prefs = PreferencesService(settings)
    assert prefs.get_library_settings().library_paths == ["/legacy/music"]


def test_scan_dirty_scopes_default_empty(prefs):
    assert prefs.get_library_scan_dirty_scopes().scope_ids == []


def test_scan_filesystem_watcher_defaults_on_with_300s_poll(prefs):
    """T15 (S-01 Hook C, D6): the poller defaults ON at 300s/60s."""
    watcher = prefs.get_library_scan_filesystem_watcher()
    assert watcher.enabled is True
    assert watcher.poll_interval_seconds == 300.0
    assert watcher.batch_window_seconds == 60.0


def test_scan_filesystem_watcher_round_trip(prefs):
    prefs.save_library_scan_filesystem_watcher(
        LibraryScanFilesystemWatcherSettings(
            enabled=False, poll_interval_seconds=60.0, batch_window_seconds=10.0
        )
    )
    reloaded = prefs.get_library_scan_filesystem_watcher()
    assert reloaded.enabled is False
    assert reloaded.poll_interval_seconds == 60.0
    assert reloaded.batch_window_seconds == 10.0


def test_scan_dirty_scopes_mark_unions_and_clear_drops_consumed(prefs):
    prefs.mark_library_scan_dirty_scopes(["root-a"])
    prefs.mark_library_scan_dirty_scopes(["root-b", "root-a"])
    assert prefs.get_library_scan_dirty_scopes().scope_ids == ["root-a", "root-b"]
    prefs.clear_library_scan_dirty_scopes(["root-a"])
    assert prefs.get_library_scan_dirty_scopes().scope_ids == ["root-b"]
    prefs.clear_library_scan_dirty_scopes(["root-b"])
    assert prefs.get_library_scan_dirty_scopes().scope_ids == []


def test_unrelated_section_saves_mark_no_dirty_scopes(prefs):
    """T16 (S-01 Hook B): schedule-text and non-library saves mark nothing."""
    prefs.save_library_scan_schedule(
        LibraryScanScheduleSettings(scan_frequency="manual")
    )
    prefs.save_library_sync_settings(LibrarySyncSettings(sync_frequency="6hr"))
    prefs.save_library_settings(LibrarySettings(library_paths=["/m"]))
    assert prefs.get_library_scan_dirty_scopes().scope_ids == []
