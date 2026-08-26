import hashlib
import json

import msgspec
import pytest

from api.v1.schemas.library_management import (
    COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID,
    LEGACY_NAMING_PROFILE_ID,
    LEGACY_NAMING_SCRIPT_ID,
    MANAGED_FIELD_NAMES,
    PICARD_ORGANIZER_PROFILE_ID,
    PICARD_ORGANIZER_MULTI_DISC_NAMING_SCRIPT_ID,
    PICARD_ORGANIZER_NAMING_SCRIPT_ID,
    LibraryManagementRootAssignment,
    LibraryManagementRootOverrides,
    LibraryManagementSettings,
    ManagedFieldSettings,
    build_initial_library_management_settings,
    normalize_library_management_settings,
    profile_revision,
    settings_revision,
)


def _round_trip(
    settings: LibraryManagementSettings,
) -> LibraryManagementSettings:
    return msgspec.convert(
        msgspec.to_builtins(settings), type=LibraryManagementSettings
    )


def test_picard_preset_is_available_but_no_root_is_activated() -> None:
    settings = build_initial_library_management_settings("{artist}/{title}.{ext}")

    assert settings.default_profile_id == PICARD_ORGANIZER_PROFILE_ID
    assert settings.root_assignments == []
    assert {profile.id for profile in settings.profiles} == {
        COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID,
        PICARD_ORGANIZER_PROFILE_ID,
        LEGACY_NAMING_PROFILE_ID,
    }
    organizer = next(
        profile
        for profile in settings.profiles
        if profile.id == PICARD_ORGANIZER_PROFILE_ID
    )
    assert {field.field for field in organizer.metadata.fields} == set(
        MANAGED_FIELD_NAMES
    )
    assert organizer.metadata.scrub_unmanaged_tags is False
    assert organizer.artwork.embedded_enabled is True
    assert organizer.artwork.external_enabled is True
    assert organizer.artwork.download_size == "full"
    assert "cover.jpg" in organizer.artwork.local_file_patterns
    assert organizer.organization.rename_enabled is True
    assert organizer.organization.move_enabled is True
    assert organizer.organization.move_sidecars is True
    assert organizer.organization.naming_script_id == PICARD_ORGANIZER_NAMING_SCRIPT_ID
    assert (
        organizer.organization.multi_disc_naming_script_id
        == PICARD_ORGANIZER_MULTI_DISC_NAMING_SCRIPT_ID
    )
    assert organizer.enrichment.lyrics.enabled is False
    assert organizer.enrichment.lyrics.write_plain is True
    assert organizer.enrichment.lyrics.write_synced is True
    assert organizer.enrichment.lyrics.preserve_existing is False
    assert organizer.enrichment.replaygain.enabled is False

    complete = next(
        profile
        for profile in settings.profiles
        if profile.id == COMPLETE_LIBRARY_ORGANIZER_PROFILE_ID
    )
    assert complete.enrichment.lyrics.enabled is True
    assert complete.enrichment.lyrics.write_plain is True
    assert complete.enrichment.lyrics.write_synced is True
    assert complete.enrichment.lyrics.preserve_existing is False
    assert complete.enrichment.lyrics.required is False
    assert complete.enrichment.replaygain.enabled is True
    assert complete.enrichment.replaygain.mode == "replace"
    assert complete.enrichment.replaygain.album_aware is True
    assert complete.enrichment.replaygain.required is False
    assert complete.genres.sources == ["musicbrainz", "listenbrainz", "lastfm"]
    assert complete.artwork.providers == [
        "cover_art_archive_release",
        "cover_art_archive_release_group",
        "local_files",
        "embedded",
    ]
    assert set(complete.artwork.image_types) == {
        "front",
        "back",
        "booklet",
        "medium",
        "tray",
        "obi",
        "spine",
        "track",
        "other",
    }
    assert complete.artwork.local_file_patterns == [
        "*.jpg",
        "*.jpeg",
        "*.png",
        "*.webp",
        "*.gif",
        "*.pdf",
    ]
    assert complete.artwork.embedded_front_only is True
    assert complete.artwork.external_front_only is False
    assert complete.metadata.scrub_unmanaged_tags is False
    assert settings.root_assignments == []


def test_legacy_template_is_copied_into_an_unassigned_path_only_profile() -> None:
    source = "{albumartist}/{album}/{track:02d} {title}.{ext}"
    settings = build_initial_library_management_settings(source)
    script = next(
        value
        for value in settings.naming_scripts
        if value.id == LEGACY_NAMING_SCRIPT_ID
    )
    profile = next(
        value for value in settings.profiles if value.id == LEGACY_NAMING_PROFILE_ID
    )

    assert script.source == source
    assert profile.organization.naming_script_id == script.id
    assert profile.metadata.enabled is False
    assert profile.genres.enabled is False
    assert profile.artwork.embedded_enabled is False
    assert profile.artwork.external_enabled is False
    assert settings.root_assignments == []


def test_nested_settings_round_trip_and_revisions_are_stable() -> None:
    settings = build_initial_library_management_settings()
    first_revision = settings_revision(settings)
    first_profiles = {profile.id: profile.revision for profile in settings.profiles}

    decoded = normalize_library_management_settings(_round_trip(settings))

    assert settings_revision(decoded) == first_revision
    assert {
        profile.id: profile.revision for profile in decoded.profiles
    } == first_profiles


def test_profile_revision_changes_when_a_capability_changes() -> None:
    settings = build_initial_library_management_settings()
    profile = next(
        value for value in settings.profiles if value.id == PICARD_ORGANIZER_PROFILE_ID
    )
    previous = profile.revision

    profile.organization.move_enabled = False
    profile.revision = profile_revision(profile)

    assert profile.revision != previous


def test_default_lyrics_preservation_keeps_pre_field_profile_revision() -> None:
    settings = build_initial_library_management_settings()
    profile = next(
        value for value in settings.profiles if value.id == PICARD_ORGANIZER_PROFILE_ID
    )
    legacy_payload = msgspec.to_builtins(profile)
    legacy_payload.pop("revision")
    legacy_payload["enrichment"]["lyrics"].pop("preserve_existing")
    legacy_revision = hashlib.sha256(
        json.dumps(
            legacy_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()

    assert profile_revision(profile) == legacy_revision

    profile.enrichment.lyrics.preserve_existing = True

    assert profile_revision(profile) != legacy_revision


def test_default_lyrics_preservation_keeps_pre_field_settings_revision() -> None:
    settings = build_initial_library_management_settings()
    legacy_payload = msgspec.to_builtins(settings)
    for profile in legacy_payload["profiles"]:
        profile["enrichment"]["lyrics"].pop("preserve_existing")
        if profile["organization"].get("multi_disc_naming_script_id") is None:
            profile["organization"].pop("multi_disc_naming_script_id")
    legacy_revision = hashlib.sha256(
        json.dumps(
            legacy_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()

    assert settings_revision(settings) == legacy_revision

    settings.profiles[0].enrichment.lyrics.preserve_existing = True

    assert settings_revision(settings) != legacy_revision


def test_null_multi_disc_field_preserves_standard_only_profile_revision() -> None:
    settings = build_initial_library_management_settings()
    profile = next(
        value for value in settings.profiles if value.id == LEGACY_NAMING_PROFILE_ID
    )
    payload = msgspec.to_builtins(profile)
    payload.pop("revision")
    payload["organization"].pop("multi_disc_naming_script_id")
    payload["enrichment"]["lyrics"].pop("preserve_existing")
    legacy_revision = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()

    assert profile.organization.multi_disc_naming_script_id is None
    assert profile_revision(profile) == legacy_revision


@pytest.mark.parametrize(
    ("mode", "script_id"),
    [
        ("inherit", PICARD_ORGANIZER_NAMING_SCRIPT_ID),
        ("standard", PICARD_ORGANIZER_NAMING_SCRIPT_ID),
        ("script", None),
    ],
)
def test_invalid_root_multi_disc_override_combinations_are_rejected(
    mode: str, script_id: str | None
) -> None:
    settings = build_initial_library_management_settings()
    settings.root_assignments = [
        LibraryManagementRootAssignment(
            root_id="root-1",
            overrides=LibraryManagementRootOverrides(
                multi_disc_naming_mode=mode,
                multi_disc_naming_script_id=script_id,
            ),
        )
    ]

    with pytest.raises(ValueError, match="multi-disc"):
        normalize_library_management_settings(settings)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda settings: settings.profiles[0].metadata.fields.append(
                ManagedFieldSettings(field="invented_field")
            ),
            "Unknown managed field",
        ),
        (
            lambda settings: settings.profiles[0].organization.sidecar_patterns.append(
                "../cover.jpg"
            ),
            "must stay inside",
        ),
        (
            lambda settings: settings.profiles[0].artwork.local_file_patterns.append(
                "**/*"
            ),
            "must stay inside",
        ),
        (
            lambda settings: setattr(
                settings.profiles[0].file_behavior, "reject_symlinks", False
            ),
            "cannot follow symlinks",
        ),
    ],
)
def test_unsafe_or_unknown_profile_values_are_rejected(mutation, message: str) -> None:
    settings = build_initial_library_management_settings()
    mutation(settings)

    with pytest.raises(ValueError, match=message):
        normalize_library_management_settings(settings)


def test_duplicate_profile_ids_are_rejected() -> None:
    settings = build_initial_library_management_settings()
    settings.profiles[1].id = settings.profiles[0].id

    with pytest.raises(ValueError, match="unique ID"):
        normalize_library_management_settings(settings)
