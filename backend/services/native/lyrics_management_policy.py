"""Shared lyrics write policy for previews and automatic imports."""

from __future__ import annotations

from collections.abc import Mapping

from api.v1.schemas.library_management import LyricsManagementSettings
from models.library_management_enrichment import LyricsProjection

LyricsOutput = tuple[str, str | None]


def _has_text(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (tuple, list)):
        return any(isinstance(item, str) and bool(item.strip()) for item in value)
    return False


def selected_lyrics_outputs(
    settings: LyricsManagementSettings,
    projection: LyricsProjection,
    *,
    synchronized_supported: bool = True,
) -> tuple[LyricsOutput, ...]:
    outputs: list[LyricsOutput] = []
    if settings.write_plain:
        outputs.append(("lyrics_plain", projection.plain_lyrics))
    if settings.write_synced and (synchronized_supported or not settings.write_plain):
        outputs.append(("lyrics_synced", projection.synced_lyrics))
    return tuple(outputs)


def required_lyrics_outputs_available(
    settings: LyricsManagementSettings,
    projection: LyricsProjection,
    existing: Mapping[str, object],
    *,
    synchronized_supported: bool = True,
) -> bool:
    outputs = selected_lyrics_outputs(
        settings, projection, synchronized_supported=synchronized_supported
    )
    return bool(outputs) and any(
        (settings.preserve_existing and _has_text(existing.get(name)))
        or (projection.status == "available" and _has_text(value))
        for name, value in outputs
    )


def planned_lyrics_outputs(
    settings: LyricsManagementSettings,
    projection: LyricsProjection,
    existing: Mapping[str, object],
    *,
    synchronized_supported: bool = True,
) -> tuple[tuple[str, str], ...]:
    if projection.status != "available":
        return ()
    return tuple(
        (name, value)
        for name, value in selected_lyrics_outputs(
            settings, projection, synchronized_supported=synchronized_supported
        )
        if isinstance(value, str)
        and value
        and not (settings.preserve_existing and _has_text(existing.get(name)))
    )


def synchronized_lyrics_supported(
    audio_format: str | None, *, wav_tag_policy: str
) -> bool:
    if audio_format == "wav":
        return wav_tag_policy != "riff_info"
    return audio_format in {"flac", "mp3", "ogg", "opus", "wma"}
