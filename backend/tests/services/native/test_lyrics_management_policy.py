from api.v1.schemas.library_management import LyricsManagementSettings
from models.library_management_enrichment import LyricsProjection
from services.native.lyrics_management_policy import (
    planned_lyrics_outputs,
    required_lyrics_outputs_available,
)


def test_default_policy_replaces_existing_selected_lyrics() -> None:
    settings = LyricsManagementSettings(enabled=True)
    projection = LyricsProjection(
        status="available",
        plain_lyrics="Provider lyrics",
        synced_lyrics="[00:01.000]Provider lyrics",
    )

    assert planned_lyrics_outputs(
        settings, projection, {"lyrics_plain": "Embedded lyrics"}
    ) == (
        ("lyrics_plain", "Provider lyrics"),
        ("lyrics_synced", "[00:01.000]Provider lyrics"),
    )


def test_preserve_policy_keeps_each_populated_output_independently() -> None:
    settings = LyricsManagementSettings(
        enabled=True,
        write_plain=True,
        write_synced=True,
        preserve_existing=True,
        required=True,
    )
    projection = LyricsProjection(
        status="available",
        plain_lyrics="Provider plain lyrics",
        synced_lyrics="[00:01.000]Provider synchronized lyrics",
    )
    existing = {"lyrics_plain": "Embedded plain lyrics"}

    assert planned_lyrics_outputs(settings, projection, existing) == (
        ("lyrics_synced", "[00:01.000]Provider synchronized lyrics"),
    )
    assert required_lyrics_outputs_available(settings, projection, existing) is True


def test_preserved_existing_output_satisfies_required_provider_degradation() -> None:
    settings = LyricsManagementSettings(
        enabled=True,
        write_synced=False,
        preserve_existing=True,
        required=True,
    )
    projection = LyricsProjection(status="deferred")

    assert (
        required_lyrics_outputs_available(
            settings, projection, {"lyrics_plain": "Embedded lyrics"}
        )
        is True
    )
    assert (
        required_lyrics_outputs_available(settings, projection, {"lyrics_plain": "   "})
        is False
    )


def test_plain_only_provider_result_satisfies_required_lyrics() -> None:
    settings = LyricsManagementSettings(
        enabled=True,
        write_plain=True,
        write_synced=True,
        required=True,
    )
    projection = LyricsProjection(status="available", plain_lyrics="Provider lyrics")

    assert required_lyrics_outputs_available(settings, projection, {}) is True


def test_synchronized_only_provider_result_satisfies_required_lyrics() -> None:
    settings = LyricsManagementSettings(
        enabled=True,
        write_plain=True,
        write_synced=True,
        required=True,
    )
    projection = LyricsProjection(
        status="available",
        synced_lyrics="[00:01.000]Provider lyrics",
    )

    assert required_lyrics_outputs_available(settings, projection, {}) is True


def test_plain_output_is_the_fallback_when_synchronized_is_unsupported() -> None:
    settings = LyricsManagementSettings(enabled=True, required=True)
    projection = LyricsProjection(
        status="available",
        plain_lyrics="Provider lyrics",
        synced_lyrics="[00:01.000]Provider lyrics",
    )

    assert planned_lyrics_outputs(
        settings,
        projection,
        {},
        synchronized_supported=False,
    ) == (("lyrics_plain", "Provider lyrics"),)
    assert (
        required_lyrics_outputs_available(
            settings,
            projection,
            {},
            synchronized_supported=False,
        )
        is True
    )


def test_synchronized_only_profile_keeps_unsupported_field_for_capability_gate() -> (
    None
):
    settings = LyricsManagementSettings(
        enabled=True,
        write_plain=False,
        write_synced=True,
    )
    projection = LyricsProjection(
        status="available",
        synced_lyrics="[00:01.000]Provider lyrics",
    )

    assert planned_lyrics_outputs(
        settings,
        projection,
        {},
        synchronized_supported=False,
    ) == (("lyrics_synced", "[00:01.000]Provider lyrics"),)
