import base64
import hashlib
import json
import zlib
from pathlib import Path

import msgspec
import pytest

from api.v1.schemas.library_management import (
    COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID,
    PICARD_ORGANIZER_PROFILE_ID,
    LibraryManagementRootAssignment,
    LibraryManagementRootOverrides,
    profile_revision,
)
from core.config import Settings
from core.exceptions import ConfigurationError, StaleRevisionError, ValidationError
from services.native.library_management_profile_service import (
    LibraryManagementProfileService,
)
from services.native.library_management_profile_sharing import (
    MAX_PROFILE_BUNDLE_BYTES,
    PROFILE_SHARE_CODE_PREFIX,
    export_profile_bundle,
    parse_profile_bundle,
    preview_materialized_profile,
    profile_import_warnings,
)
from models.library_management_planning import (
    naming_policy_revision,
    pin_library_management_profile,
)
from services.native.library_policy_resolver import LibraryPolicyResolver
from services.preferences_service import PreferencesService


def _service(tmp_path: Path) -> LibraryManagementProfileService:
    root = tmp_path / "Music"
    root.mkdir()
    settings = Settings()
    settings.config_file_path = tmp_path / "config.json"
    settings.config_file_path.write_text(
        json.dumps({"library_settings": {"library_paths": [str(root)]}}),
        encoding="utf-8",
    )
    return LibraryManagementProfileService(PreferencesService(settings))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _rechecksum(document: dict[str, object]) -> str:
    payload = document["payload"]
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    document["checksum"] = f"sha256:{digest}"
    return json.dumps(document)


def test_export_is_deterministic_and_file_and_code_round_trip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    settings = service.get_settings()

    first = service.export_profile(
        PICARD_ORGANIZER_PROFILE_ID,
        expected_settings_revision=settings.settings_revision,
    )
    second = service.export_profile(
        PICARD_ORGANIZER_PROFILE_ID,
        expected_settings_revision=settings.settings_revision,
    )

    assert first == second
    assert first.filename == "picard-style-organizer.dnprofile"
    assert first.mime_type == "application/vnd.droppedneedle.profile+json"
    assert first.share_code.startswith(PROFILE_SHARE_CODE_PREFIX)
    assert parse_profile_bundle(first.document).bundle_hash == first.bundle_hash
    assert parse_profile_bundle(first.share_code).bundle_hash == first.bundle_hash
    document = json.loads(first.document)
    assert document["format"] == "droppedneedle-library-profile"
    assert document["version"] == 1
    assert set(document["payload"]["profile"]).isdisjoint(
        {"id", "revision", "preset_origin", "preset_version"}
    )
    assert "refresh_droppedneedle" not in document["payload"]["profile"]["notification"]


def test_bundle_preserves_unicode_and_all_referenced_scripts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    settings = service._preferences.get_library_management_settings_raw()
    profile = next(
        value
        for value in settings.profiles
        if value.id == COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID
    )
    profile.name = "Björk 日本語"
    script = next(
        value
        for value in settings.naming_scripts
        if value.id == profile.organization.naming_script_id
    )
    script.name = "Álbum folders"

    bundle = export_profile_bundle(
        profile, settings.naming_scripts, settings.tagging_scripts
    )
    materialized = preview_materialized_profile(parse_profile_bundle(bundle.document))

    assert materialized.profile.name == "Björk 日本語"
    assert {value.name for value in materialized.naming_scripts} >= {"Álbum folders"}
    assert len(materialized.naming_scripts) == 2


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(version=2), "Unsupported profile bundle version"),
        (
            lambda value: value["payload"]["profile"].update(future_setting=True),
            "Unknown or unsupported profile field",
        ),
        (
            lambda value: value["payload"]["profile"]["metadata"][
                "format_compatibility"
            ].update(constrained_genres_primary_only=True),
            "Unknown or unsupported profile field",
        ),
    ],
)
def test_bundle_rejects_versions_and_unknown_or_dead_fields(
    tmp_path: Path, mutate, message: str
) -> None:
    service = _service(tmp_path)
    settings = service.get_settings()
    exported = service.export_profile(
        PICARD_ORGANIZER_PROFILE_ID,
        expected_settings_revision=settings.settings_revision,
    )
    document = json.loads(exported.document)
    mutate(document)
    content = _rechecksum(document)

    with pytest.raises(ValidationError, match=message):
        parse_profile_bundle(content)


def test_bundle_rejects_checksum_dependency_and_code_corruption(tmp_path: Path) -> None:
    service = _service(tmp_path)
    settings = service.get_settings()
    exported = service.export_profile(
        PICARD_ORGANIZER_PROFILE_ID,
        expected_settings_revision=settings.settings_revision,
    )
    mismatched = json.loads(exported.document)
    mismatched["payload"]["profile"]["description"] = "Changed"
    with pytest.raises(ValidationError, match="checksum"):
        parse_profile_bundle(json.dumps(mismatched))

    missing = json.loads(exported.document)
    missing["payload"]["naming_scripts"].pop()
    parsed = parse_profile_bundle(_rechecksum(missing))
    with pytest.raises(ValidationError, match="missing or unreferenced"):
        preview_materialized_profile(parsed)

    with pytest.raises(ValidationError, match="malformed|corrupted"):
        parse_profile_bundle(f"{PROFILE_SHARE_CODE_PREFIX}not-valid!")
    compressed = zlib.compress(exported.document.encode()) + b"trailing"
    encoded = base64.urlsafe_b64encode(compressed).rstrip(b"=").decode()
    with pytest.raises(ValidationError, match="trailing"):
        parse_profile_bundle(f"{PROFILE_SHARE_CODE_PREFIX}{encoded}")


def test_bundle_rejects_oversized_plain_document() -> None:
    with pytest.raises(ValidationError, match="too large"):
        parse_profile_bundle("x" * (MAX_PROFILE_BUNDLE_BYTES + 1))


def test_bundle_rejects_excessive_json_nesting() -> None:
    depth = 20_000
    nested = "[" * depth + '"value"' + "]" * depth
    payload = (
        '{"naming_scripts":[],"profile":{"description":'
        + nested
        + '},"tagging_scripts":[]}'
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    document = (
        '{"checksum":"sha256:'
        + digest
        + '","format":"droppedneedle-library-profile","payload":'
        + payload
        + ',"version":1}'
    )

    with pytest.raises(ValidationError, match="valid versioned JSON"):
        parse_profile_bundle(document)


def test_preview_resolves_conflicts_without_writing(tmp_path: Path) -> None:
    service = _service(tmp_path)
    before = service.get_settings()
    exported = service.export_profile(
        COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID,
        expected_settings_revision=before.settings_revision,
    )

    preview = service.preview_profile_import(
        exported.document,
        expected_settings_revision=before.settings_revision,
    )
    after = service.get_settings()

    assert preview.profile.name == "Complete Library Organizer (imported)"
    assert all("(imported)" in script.name for script in preview.naming_scripts)
    assert {warning.code for warning in preview.warnings} >= {
        "remove_sources",
        "replace_enrichment",
    }
    assert "ReplayGain" in preview.aspects
    assert after == before


def test_preview_suffixes_maximum_length_names(tmp_path: Path) -> None:
    service = _service(tmp_path)
    current = service.get_settings()
    proposed = service._preferences.get_library_management_settings_raw()
    profile = next(
        value for value in proposed.profiles if value.id == PICARD_ORGANIZER_PROFILE_ID
    )
    profile.name = "P" * 120
    referenced_ids = {
        profile.organization.naming_script_id,
        profile.organization.multi_disc_naming_script_id,
    }
    referenced_scripts = [
        script for script in proposed.naming_scripts if script.id in referenced_ids
    ]
    for index, script in enumerate(referenced_scripts):
        script.name = chr(ord("N") + index) * 120
    saved = service.save_settings(
        proposed,
        expected_settings_revision=current.settings_revision,
    )
    exported = service.export_profile(
        profile.id,
        expected_settings_revision=saved.settings_revision,
    )

    preview = service.preview_profile_import(
        exported.document,
        expected_settings_revision=saved.settings_revision,
    )

    assert len(preview.profile.name) == 120
    assert preview.profile.name.endswith(" (imported)")
    assert all(len(script.name) == 120 for script in preview.naming_scripts)
    assert all(script.name.endswith(" (imported)") for script in preview.naming_scripts)


@pytest.mark.parametrize(
    ("field", "value", "warning_code"),
    [
        ("remove_id3_from_flac", True, "remove_flac_id3"),
        ("mp3_apev2_policy", "remove", "remove_mp3_apev2"),
        ("raw_aac_tag_policy", "remove_apev2", "remove_raw_aac_apev2"),
        ("raw_aac_tag_policy", "do_not_write", "skip_raw_aac_tags"),
        ("wav_tag_policy", "riff_info", "convert_wav_tags"),
    ],
)
def test_preview_warns_about_format_compatibility_policies(
    tmp_path: Path,
    field: str,
    value: object,
    warning_code: str,
) -> None:
    service = _service(tmp_path)
    settings = service._preferences.get_library_management_settings_raw()
    profile = next(
        item for item in settings.profiles if item.id == PICARD_ORGANIZER_PROFILE_ID
    )
    profile.metadata.format_compatibility.wav_tag_policy = "preserve_existing"
    setattr(profile.metadata.format_compatibility, field, value)

    assert warning_code in {
        warning.code for warning in profile_import_warnings(profile)
    }


def test_import_creates_an_inert_custom_profile_with_fresh_dependencies(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    before = service.get_settings()
    exported = service.export_profile(
        COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID,
        expected_settings_revision=before.settings_revision,
    )
    preview = service.preview_profile_import(
        exported.document,
        expected_settings_revision=before.settings_revision,
    )

    imported = service.import_profile(
        exported.document,
        reviewed_bundle_hash=preview.bundle_hash,
        name=preview.profile.name,
        expected_settings_revision=before.settings_revision,
    )
    saved = service.get_settings()

    assert imported.profile.id not in {profile.id for profile in before.profiles}
    assert imported.profile.preset_origin is None
    assert imported.profile.preset_version is None
    assert saved.default_profile_id == before.default_profile_id
    assert saved.root_assignments == before.root_assignments
    assert imported.profile.organization.naming_script_id in {
        script.id for script in imported.naming_scripts
    }
    assert imported.profile.organization.multi_disc_naming_script_id in {
        script.id for script in imported.naming_scripts
    }
    assert {script.id for script in imported.naming_scripts}.isdisjoint(
        {script.id for script in before.naming_scripts}
    )


def test_deleting_import_removes_only_its_unreferenced_custom_scripts(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    before = service.get_settings()
    exported = service.export_profile(
        COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID,
        expected_settings_revision=before.settings_revision,
    )
    imported = service.import_profile(
        exported.document,
        reviewed_bundle_hash=exported.bundle_hash,
        name="Temporary shared profile",
        expected_settings_revision=before.settings_revision,
    )
    imported_naming_ids = {script.id for script in imported.naming_scripts}
    imported_tagging_ids = {script.id for script in imported.tagging_scripts}

    saved = service.delete_profile(
        imported.profile.id,
        expected_settings_revision=imported.settings_revision,
    )

    assert imported_naming_ids.isdisjoint(
        {script.id for script in saved.naming_scripts}
    )
    assert imported_tagging_ids.isdisjoint(
        {script.id for script in saved.tagging_scripts}
    )
    assert {script.id for script in before.naming_scripts} <= {
        script.id for script in saved.naming_scripts
    }


def test_deleting_import_keeps_a_custom_script_used_by_another_profile(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    before = service.get_settings()
    exported = service.export_profile(
        PICARD_ORGANIZER_PROFILE_ID,
        expected_settings_revision=before.settings_revision,
    )
    imported = service.import_profile(
        exported.document,
        reviewed_bundle_hash=exported.bundle_hash,
        name="Shared dependency source",
        expected_settings_revision=before.settings_revision,
    )
    copied = service.copy_profile(
        COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID,
        name="Shared dependency user",
        expected_settings_revision=imported.settings_revision,
    )
    current = service.get_settings()
    copied.organization.naming_script_id = imported.naming_scripts[0].id
    copied = service.update_profile(
        copied,
        expected_settings_revision=current.settings_revision,
    )
    current = service.get_settings()

    saved = service.delete_profile(
        imported.profile.id,
        expected_settings_revision=current.settings_revision,
    )

    assert copied.organization.naming_script_id in {
        script.id for script in saved.naming_scripts
    }


def test_deleting_import_keeps_a_custom_script_used_by_a_root_override(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    before = service.get_settings()
    exported = service.export_profile(
        PICARD_ORGANIZER_PROFILE_ID,
        expected_settings_revision=before.settings_revision,
    )
    imported = service.import_profile(
        exported.document,
        reviewed_bundle_hash=exported.bundle_hash,
        name="Root override script source",
        expected_settings_revision=before.settings_revision,
    )
    override_script_id = imported.naming_scripts[0].id
    proposed = service._preferences.get_library_management_settings_raw()
    root_id = service._preferences.get_typed_library_settings_raw().library_roots[0].id
    proposed.root_assignments = [
        LibraryManagementRootAssignment(
            root_id=root_id,
            profile_id=PICARD_ORGANIZER_PROFILE_ID,
            overrides=LibraryManagementRootOverrides(
                naming_script_id=override_script_id,
            ),
        )
    ]
    assigned = service.save_settings(
        proposed,
        expected_settings_revision=imported.settings_revision,
    )

    saved = service.delete_profile(
        imported.profile.id,
        expected_settings_revision=assigned.settings_revision,
    )

    assert override_script_id in {script.id for script in saved.naming_scripts}


def test_import_does_not_stale_an_unaffected_active_assignment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    preferences = service._preferences
    current = service.get_settings()
    proposed = preferences.get_library_management_settings_raw()
    profile = next(
        value for value in proposed.profiles if value.id == PICARD_ORGANIZER_PROFILE_ID
    )
    root_id = preferences.get_typed_library_settings_raw().library_roots[0].id
    policy_revision = LibraryPolicyResolver(
        preferences.get_typed_library_settings_raw()
    ).policy_revision
    proposed.root_assignments = [
        LibraryManagementRootAssignment(
            root_id=root_id,
            enabled=True,
            automatic_acquisitions=True,
            activation_profile_revision=profile_revision(profile),
            activation_naming_policy_revision=naming_policy_revision(
                pin_library_management_profile(proposed, profile)
            ),
            activation_policy_revision=policy_revision,
            activation_settings_revision=current.settings_revision,
            activation_preview_token="verified",
            activation_preview_hash="preview-hash",
            activation_confirmed_at=1.0,
        )
    ]
    activated = service.save_settings(
        proposed,
        expected_settings_revision=current.settings_revision,
        validated_activation_root_ids=frozenset({root_id}),
    )
    exported = service.export_profile(
        COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID,
        expected_settings_revision=activated.settings_revision,
    )
    preview = service.preview_profile_import(
        exported.document,
        expected_settings_revision=activated.settings_revision,
    )

    imported = service.import_profile(
        exported.document,
        reviewed_bundle_hash=preview.bundle_hash,
        name=preview.profile.name,
        expected_settings_revision=activated.settings_revision,
    )
    prepared = service.prepare_automatic_profile(
        root_id=root_id,
        trigger="acquisition",
        expected_policy_revision=policy_revision,
    )

    assert imported.profile.id != PICARD_ORGANIZER_PROFILE_ID
    assert prepared is not None
    assert prepared[2].id == PICARD_ORGANIZER_PROFILE_ID


def test_repeated_imports_suffix_names_deterministically(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first_settings = service.get_settings()
    exported = service.export_profile(
        PICARD_ORGANIZER_PROFILE_ID,
        expected_settings_revision=first_settings.settings_revision,
    )
    first_preview = service.preview_profile_import(
        exported.document,
        expected_settings_revision=first_settings.settings_revision,
    )
    first = service.import_profile(
        exported.document,
        reviewed_bundle_hash=first_preview.bundle_hash,
        name=first_preview.profile.name,
        expected_settings_revision=first_settings.settings_revision,
    )
    second_preview = service.preview_profile_import(
        exported.document,
        expected_settings_revision=first.settings_revision,
    )

    assert first.profile.name == "Picard-style Organizer (imported)"
    assert second_preview.profile.name == "Picard-style Organizer (imported 2)"
    assert all(
        "(imported 2)" in script.name for script in second_preview.naming_scripts
    )


def test_stale_or_changed_import_fails_without_writing(tmp_path: Path) -> None:
    service = _service(tmp_path)
    before = service.get_settings()
    exported = service.export_profile(
        PICARD_ORGANIZER_PROFILE_ID,
        expected_settings_revision=before.settings_revision,
    )

    with pytest.raises(StaleRevisionError, match="changed after"):
        service.import_profile(
            exported.document,
            reviewed_bundle_hash="0" * 64,
            name="Imported organizer",
            expected_settings_revision=before.settings_revision,
        )
    with pytest.raises(StaleRevisionError, match="Refresh"):
        service.preview_profile_import(
            exported.document,
            expected_settings_revision="stale",
        )
    assert service.get_settings() == before


def test_conflicting_chosen_name_fails_atomically(tmp_path: Path) -> None:
    service = _service(tmp_path)
    before = service.get_settings()
    exported = service.export_profile(
        PICARD_ORGANIZER_PROFILE_ID,
        expected_settings_revision=before.settings_revision,
    )

    with pytest.raises(ConfigurationError, match="unique name"):
        service.import_profile(
            exported.document,
            reviewed_bundle_hash=exported.bundle_hash,
            name="Complete Library Organizer",
            expected_settings_revision=before.settings_revision,
        )
    assert service.get_settings() == before
