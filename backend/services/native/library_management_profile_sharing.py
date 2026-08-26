"""Deterministic, inert Library Management profile sharing bundles."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass

import msgspec

from api.v1.schemas.library_management import (
    MAX_MANAGEMENT_NAME_LENGTH,
    LibraryManagementProfile,
    LibraryManagementSettings,
    NamingScriptSettings,
    TaggingScriptSettings,
    normalize_library_management_settings,
)
from api.v1.schemas.library_management_sharing import (
    LibraryManagementProfileImportWarning,
)
from core.exceptions import ValidationError

PROFILE_BUNDLE_FORMAT = "droppedneedle-library-profile"
PROFILE_BUNDLE_VERSION = 1
PROFILE_BUNDLE_MIME_TYPE = "application/vnd.droppedneedle.profile+json"
PROFILE_SHARE_CODE_PREFIX = "DNLP1:"
MAX_PROFILE_BUNDLE_BYTES = 1_048_576
MAX_PROFILE_SHARE_CODE_CHARS = 1_500_000
_PREVIEW_NAMESPACE = uuid.UUID("54f7ffdf-3c94-58d4-8c08-7a21304ea02d")
_IDENTITY_FIELDS = {"id", "preset_origin", "preset_version", "revision"}
_COMPATIBILITY_ONLY_FIELDS = {
    ("metadata", "format_compatibility", "constrained_genres_primary_only"),
    ("notification", "refresh_droppedneedle"),
}


class _PortableScript(msgspec.Struct, forbid_unknown_fields=True, kw_only=True):
    key: str
    name: str
    source: str


class _PortablePayload(msgspec.Struct, forbid_unknown_fields=True, kw_only=True):
    profile: dict[str, object]
    naming_scripts: list[_PortableScript] = msgspec.field(default_factory=list)
    tagging_scripts: list[_PortableScript] = msgspec.field(default_factory=list)


class _PortableDocument(msgspec.Struct, forbid_unknown_fields=True, kw_only=True):
    format: str
    version: int
    payload: _PortablePayload
    checksum: str


@dataclass(frozen=True)
class EncodedProfileBundle:
    document: str
    share_code: str
    bundle_hash: str


@dataclass(frozen=True)
class ParsedProfileBundle:
    payload: _PortablePayload
    bundle_hash: str


@dataclass(frozen=True)
class MaterializedProfileBundle:
    profile: LibraryManagementProfile
    naming_scripts: list[NamingScriptSettings]
    tagging_scripts: list[TaggingScriptSettings]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _bundle_hash(payload: _PortablePayload) -> str:
    return hashlib.sha256(_canonical_json(msgspec.to_builtins(payload))).hexdigest()


def _ordered_unique(values: list[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _portable_profile(
    profile: LibraryManagementProfile,
    naming_keys: dict[str, str],
    tagging_keys: dict[str, str],
) -> dict[str, object]:
    value = msgspec.to_builtins(profile)
    if not isinstance(value, dict):
        raise TypeError("Profile sharing requires an object profile.")
    for field in _IDENTITY_FIELDS:
        value.pop(field, None)
    metadata = value["metadata"]
    artwork = value["artwork"]
    organization = value["organization"]
    notification = value["notification"]
    assert isinstance(metadata, dict)
    assert isinstance(artwork, dict)
    assert isinstance(organization, dict)
    assert isinstance(notification, dict)
    compatibility = metadata["format_compatibility"]
    assert isinstance(compatibility, dict)
    compatibility.pop("constrained_genres_primary_only", None)
    notification.pop("refresh_droppedneedle", None)
    metadata["tagging_script_ids"] = [
        tagging_keys[value] for value in profile.metadata.tagging_script_ids
    ]
    organization["naming_script_id"] = naming_keys[
        profile.organization.naming_script_id
    ]
    organization["multi_disc_naming_script_id"] = (
        naming_keys[profile.organization.multi_disc_naming_script_id]
        if profile.organization.multi_disc_naming_script_id is not None
        else None
    )
    artwork["external_naming_script_id"] = (
        naming_keys[profile.artwork.external_naming_script_id]
        if profile.artwork.external_naming_script_id is not None
        else None
    )
    return value


def export_profile_bundle(
    profile: LibraryManagementProfile,
    naming_scripts: list[NamingScriptSettings],
    tagging_scripts: list[TaggingScriptSettings],
) -> EncodedProfileBundle:
    naming_by_id = {script.id: script for script in naming_scripts}
    tagging_by_id = {script.id: script for script in tagging_scripts}
    naming_ids = _ordered_unique(
        [
            profile.organization.naming_script_id,
            profile.organization.multi_disc_naming_script_id,
            profile.artwork.external_naming_script_id,
        ]
    )
    tagging_ids = _ordered_unique(list(profile.metadata.tagging_script_ids))
    try:
        selected_naming = [naming_by_id[value] for value in naming_ids]
        selected_tagging = [tagging_by_id[value] for value in tagging_ids]
    except KeyError as exc:
        raise ValidationError(
            "The profile references a script that is no longer available."
        ) from exc
    naming_keys = {
        script.id: f"naming-{index}"
        for index, script in enumerate(selected_naming, start=1)
    }
    tagging_keys = {
        script.id: f"tagging-{index}"
        for index, script in enumerate(selected_tagging, start=1)
    }
    payload = _PortablePayload(
        profile=_portable_profile(profile, naming_keys, tagging_keys),
        naming_scripts=[
            _PortableScript(
                key=naming_keys[script.id], name=script.name, source=script.source
            )
            for script in selected_naming
        ],
        tagging_scripts=[
            _PortableScript(
                key=tagging_keys[script.id], name=script.name, source=script.source
            )
            for script in selected_tagging
        ],
    )
    digest = _bundle_hash(payload)
    document_value = {
        "format": PROFILE_BUNDLE_FORMAT,
        "version": PROFILE_BUNDLE_VERSION,
        "payload": msgspec.to_builtins(payload),
        "checksum": f"sha256:{digest}",
    }
    document_bytes = (
        json.dumps(document_value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(document_bytes) > MAX_PROFILE_BUNDLE_BYTES:
        raise ValidationError("This profile is too large to share.")
    compressed = zlib.compress(_canonical_json(document_value), level=9)
    share_code = PROFILE_SHARE_CODE_PREFIX + base64.urlsafe_b64encode(
        compressed
    ).rstrip(b"=").decode("ascii")
    if len(share_code) > MAX_PROFILE_SHARE_CODE_CHARS:
        raise ValidationError("This profile is too large to encode as a share code.")
    return EncodedProfileBundle(
        document=document_bytes.decode("utf-8"),
        share_code=share_code,
        bundle_hash=digest,
    )


def _decode_share_code(content: str) -> bytes:
    encoded = content[len(PROFILE_SHARE_CODE_PREFIX) :]
    if not encoded or len(content) > MAX_PROFILE_SHARE_CODE_CHARS:
        raise ValidationError("The profile share code is empty or too large.")
    padding = "=" * (-len(encoded) % 4)
    try:
        compressed = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise ValidationError("The profile share code is malformed.") from exc
    decompressor = zlib.decompressobj()
    try:
        decoded = decompressor.decompress(compressed, MAX_PROFILE_BUNDLE_BYTES + 1)
        if len(decoded) > MAX_PROFILE_BUNDLE_BYTES or decompressor.unconsumed_tail:
            raise ValidationError("The decoded profile bundle is too large.")
        decoded += decompressor.flush(MAX_PROFILE_BUNDLE_BYTES + 1 - len(decoded))
    except zlib.error as exc:
        raise ValidationError("The profile share code is corrupted.") from exc
    if len(decoded) > MAX_PROFILE_BUNDLE_BYTES:
        raise ValidationError("The decoded profile bundle is too large.")
    if not decompressor.eof or decompressor.unused_data:
        raise ValidationError(
            "The profile share code contains trailing or incomplete data."
        )
    return decoded


def _profile_schema_node() -> tuple[dict[str, object], dict[str, object]]:
    schema = msgspec.json.schema(LibraryManagementProfile)
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise RuntimeError(
            "Library Management profile schema definitions are unavailable."
        )
    profile_schema = definitions.get("LibraryManagementProfile")
    if not isinstance(profile_schema, dict):
        raise RuntimeError("Library Management profile schema is unavailable.")
    return profile_schema, definitions


def _reject_unknown_profile_fields(
    value: object,
    schema: dict[str, object],
    definitions: dict[str, object],
    path: tuple[str, ...] = (),
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        target = definitions.get(reference.rsplit("/", maxsplit=1)[-1])
        if isinstance(target, dict):
            _reject_unknown_profile_fields(value, target, definitions, path)
        return
    if isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return
        allowed = set(properties)
        if not path:
            allowed -= _IDENTITY_FIELDS
        for blocked_path in _COMPATIBILITY_ONLY_FIELDS:
            if blocked_path[:-1] == path:
                allowed.discard(blocked_path[-1])
        unknown = set(value) - allowed
        if unknown:
            location = ".".join((*path, sorted(unknown)[0]))
            raise ValidationError(f"Unknown or unsupported profile field: {location}")
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, dict):
                _reject_unknown_profile_fields(item, child, definitions, (*path, key))
        return
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for item in value:
                _reject_unknown_profile_fields(item, item_schema, definitions, path)
        return
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        for alternative in alternatives:
            if isinstance(alternative, dict) and (
                (
                    isinstance(value, dict)
                    and ("properties" in alternative or "$ref" in alternative)
                )
                or (isinstance(value, list) and alternative.get("type") == "array")
            ):
                _reject_unknown_profile_fields(value, alternative, definitions, path)
                return


def parse_profile_bundle(content: str) -> ParsedProfileBundle:
    stripped = content.strip()
    if stripped.startswith(PROFILE_SHARE_CODE_PREFIX):
        document_bytes = _decode_share_code(stripped)
    else:
        document_bytes = stripped.encode("utf-8")
        if not document_bytes:
            raise ValidationError("Paste a profile code or choose a .dnprofile file.")
        if len(document_bytes) > MAX_PROFILE_BUNDLE_BYTES:
            raise ValidationError("The profile bundle is too large.")
    try:
        raw = msgspec.json.decode(document_bytes)
        document = msgspec.convert(raw, type=_PortableDocument, strict=True)
    except (
        msgspec.DecodeError,
        msgspec.ValidationError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValidationError(
            "The profile bundle is not valid versioned JSON."
        ) from exc
    if document.format != PROFILE_BUNDLE_FORMAT:
        raise ValidationError(
            "This file is not a DroppedNeedle Library Management profile."
        )
    if document.version != PROFILE_BUNDLE_VERSION:
        raise ValidationError(
            f"Unsupported profile bundle version: {document.version}."
        )
    profile_schema, definitions = _profile_schema_node()
    _reject_unknown_profile_fields(
        document.payload.profile, profile_schema, definitions
    )
    digest = _bundle_hash(document.payload)
    if document.checksum != f"sha256:{digest}":
        raise ValidationError(
            "The profile bundle checksum does not match its contents."
        )
    return ParsedProfileBundle(payload=document.payload, bundle_hash=digest)


def _validate_dependency_collection(
    scripts: list[_PortableScript],
    referenced: list[str],
    label: str,
) -> None:
    keys = [script.key for script in scripts]
    if any(not key or len(key) > 64 for key in keys):
        raise ValidationError(f"A {label} key is invalid.")
    if len(set(keys)) != len(keys):
        raise ValidationError(f"The profile bundle contains duplicate {label} keys.")
    if len({script.name.casefold() for script in scripts}) != len(scripts):
        raise ValidationError(f"The profile bundle contains duplicate {label} names.")
    if set(keys) != set(referenced):
        raise ValidationError(
            f"The profile bundle has missing or unreferenced {label} dependencies."
        )


def _materialized_profile_value(
    parsed: ParsedProfileBundle,
    profile_id: str,
    naming_ids: dict[str, str],
    tagging_ids: dict[str, str],
) -> dict[str, object]:
    value = msgspec.to_builtins(parsed.payload.profile)
    if not isinstance(value, dict):
        raise ValidationError("The shared profile is not an object.")
    metadata = value.get("metadata")
    organization = value.get("organization")
    artwork = value.get("artwork")
    if not isinstance(metadata, dict) or not isinstance(organization, dict):
        raise ValidationError("The shared profile is missing script assignments.")
    if not isinstance(artwork, dict):
        artwork = {}
        value["artwork"] = artwork
    tagging_keys = metadata.get("tagging_script_ids", [])
    naming_key = organization.get("naming_script_id")
    multi_disc_key = organization.get("multi_disc_naming_script_id")
    external_key = artwork.get("external_naming_script_id")
    if not isinstance(tagging_keys, list) or not all(
        isinstance(key, str) for key in tagging_keys
    ):
        raise ValidationError("The shared tagging-script assignments are invalid.")
    if not isinstance(naming_key, str):
        raise ValidationError("The shared naming-script assignment is invalid.")
    referenced_naming = [naming_key]
    for key in (multi_disc_key, external_key):
        if key is not None:
            if not isinstance(key, str):
                raise ValidationError("A shared naming-script assignment is invalid.")
            referenced_naming.append(key)
    _validate_dependency_collection(
        parsed.payload.naming_scripts,
        _ordered_unique(referenced_naming),
        "naming script",
    )
    _validate_dependency_collection(
        parsed.payload.tagging_scripts,
        _ordered_unique(tagging_keys),
        "tagging script",
    )
    try:
        metadata["tagging_script_ids"] = [tagging_ids[key] for key in tagging_keys]
        organization["naming_script_id"] = naming_ids[naming_key]
        organization["multi_disc_naming_script_id"] = (
            naming_ids[multi_disc_key] if multi_disc_key is not None else None
        )
        artwork["external_naming_script_id"] = (
            naming_ids[external_key] if external_key is not None else None
        )
    except KeyError as exc:
        raise ValidationError(
            "The shared profile references a missing script."
        ) from exc
    value.update(
        {
            "id": profile_id,
            "preset_origin": None,
            "preset_version": None,
            "revision": "",
        }
    )
    return value


def materialize_profile_bundle(
    parsed: ParsedProfileBundle,
    *,
    profile_id: str,
    naming_id_factory: Callable[[_PortableScript], str],
    tagging_id_factory: Callable[[_PortableScript], str],
) -> MaterializedProfileBundle:
    naming_ids = {
        script.key: naming_id_factory(script)
        for script in parsed.payload.naming_scripts
    }
    tagging_ids = {
        script.key: tagging_id_factory(script)
        for script in parsed.payload.tagging_scripts
    }
    try:
        profile = msgspec.convert(
            _materialized_profile_value(parsed, profile_id, naming_ids, tagging_ids),
            type=LibraryManagementProfile,
            strict=True,
        )
    except (msgspec.ValidationError, TypeError, ValueError) as exc:
        raise ValidationError("The shared profile contains invalid settings.") from exc
    if any(field.mode == "preserve" for field in profile.metadata.fields):
        raise ValidationError("Legacy Preserve metadata modes cannot be imported.")
    if profile.metadata.artist_credits.standardization == "variations":
        raise ValidationError("Legacy artist-variation settings cannot be imported.")
    if "audiodb" in profile.artwork.providers:
        raise ValidationError("TheAudioDB artwork settings cannot be imported.")
    naming_scripts = [
        NamingScriptSettings(
            id=naming_ids[script.key],
            name=script.name,
            source=script.source,
        )
        for script in parsed.payload.naming_scripts
    ]
    tagging_scripts = [
        TaggingScriptSettings(
            id=tagging_ids[script.key],
            name=script.name,
            source=script.source,
        )
        for script in parsed.payload.tagging_scripts
    ]
    try:
        normalized = normalize_library_management_settings(
            LibraryManagementSettings(
                profiles=[profile],
                default_profile_id=profile.id,
                naming_scripts=naming_scripts,
                tagging_scripts=tagging_scripts,
            )
        )
    except (msgspec.ValidationError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc
    return MaterializedProfileBundle(
        profile=normalized.profiles[0],
        naming_scripts=normalized.naming_scripts,
        tagging_scripts=normalized.tagging_scripts,
    )


def preview_materialized_profile(
    parsed: ParsedProfileBundle,
) -> MaterializedProfileBundle:
    def stable_id(kind: str, key: str) -> str:
        return str(uuid.uuid5(_PREVIEW_NAMESPACE, f"{parsed.bundle_hash}:{kind}:{key}"))

    return materialize_profile_bundle(
        parsed,
        profile_id=stable_id("profile", "profile"),
        naming_id_factory=lambda script: stable_id("naming", script.key),
        tagging_id_factory=lambda script: stable_id("tagging", script.key),
    )


def unique_import_name(base: str, used_names: set[str]) -> str:
    if base.casefold() not in used_names:
        return base
    number: int | None = None
    while True:
        suffix = " (imported)" if number is None else f" (imported {number})"
        prefix = base[: MAX_MANAGEMENT_NAME_LENGTH - len(suffix)].rstrip()
        candidate = f"{prefix}{suffix}"
        if candidate.casefold() not in used_names:
            return candidate
        number = 2 if number is None else number + 1


def profile_bundle_filename(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")
    return f"{slug or 'library-profile'}.dnprofile"


def profile_aspects(profile: LibraryManagementProfile) -> list[str]:
    aspects: list[str] = []
    if profile.metadata.enabled:
        aspects.append("Metadata tags")
    if profile.genres.enabled:
        aspects.append("Genres")
    if profile.artwork.embedded_enabled or profile.artwork.external_enabled:
        aspects.append("Artwork")
    if profile.enrichment.lyrics.enabled:
        aspects.append("Lyrics")
    if profile.enrichment.replaygain.enabled:
        aspects.append("ReplayGain")
    if profile.organization.rename_enabled:
        aspects.append("Rename files")
    if profile.organization.move_enabled:
        aspects.append("Move files")
    return aspects


def profile_import_warnings(
    profile: LibraryManagementProfile,
) -> list[LibraryManagementProfileImportWarning]:
    warnings: list[LibraryManagementProfileImportWarning] = []
    if profile.metadata.enabled and profile.metadata.scrub_unmanaged_tags:
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="scrub_unmanaged_tags",
                severity="danger",
                title="Removes unmanaged tags",
                message="Tags outside the managed and preserved field lists will be removed.",
            )
        )
    if profile.metadata.enabled and any(
        field.mode == "replace" and field.clear_when_canonical_missing
        for field in profile.metadata.fields
    ):
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="clear_missing_metadata",
                severity="danger",
                title="Clears missing canonical values",
                message="Selected Replace fields may be cleared when MusicBrainz has no value.",
            )
        )
    compatibility = profile.metadata.format_compatibility
    if profile.metadata.enabled and compatibility.remove_id3_from_flac:
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="remove_flac_id3",
                severity="danger",
                title="Removes ID3 tags from FLAC files",
                message="Any stray ID3 tag container is deleted when a FLAC file is written.",
            )
        )
    if profile.metadata.enabled and compatibility.mp3_apev2_policy == "remove":
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="remove_mp3_apev2",
                severity="danger",
                title="Removes APEv2 tags from MP3 files",
                message="The complete MP3 APEv2 tag container is deleted.",
            )
        )
    if profile.metadata.enabled and compatibility.raw_aac_tag_policy == "remove_apev2":
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="remove_raw_aac_apev2",
                severity="danger",
                title="Removes APEv2 tags from raw AAC files",
                message="The APEv2 tag container and any artwork stored there are deleted.",
            )
        )
    elif (
        profile.metadata.enabled and compatibility.raw_aac_tag_policy == "do_not_write"
    ):
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="skip_raw_aac_tags",
                severity="warning",
                title="Does not write tags to raw AAC files",
                message="Managed metadata changes are skipped for raw AAC files.",
            )
        )
    if profile.metadata.enabled and compatibility.wav_tag_policy != "preserve_existing":
        wav_format = "ID3" if compatibility.wav_tag_policy == "id3" else "RIFF INFO"
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="convert_wav_tags",
                severity="warning",
                title="May convert WAV tags",
                message=(
                    f"WAV metadata is written as {wav_format}, "
                    "which may replace the current tag representation."
                ),
            )
        )
    if (
        profile.organization.move_enabled
        and profile.organization.source_cleanup == "remove_after_confirmed_move"
    ):
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="remove_sources",
                severity="danger",
                title="Removes verified move sources",
                message="Source files are removed after their managed moves are confirmed.",
            )
        )
    if profile.artwork.external_enabled and profile.artwork.overwrite_external_files:
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="overwrite_external_artwork",
                severity="danger",
                title="Overwrites external artwork",
                message="Existing external artwork files may be replaced.",
            )
        )
    replacement_enrichment: list[str] = []
    if (
        profile.enrichment.lyrics.enabled
        and not profile.enrichment.lyrics.preserve_existing
    ):
        replacement_enrichment.append("lyrics")
    if (
        profile.enrichment.replaygain.enabled
        and profile.enrichment.replaygain.mode == "replace"
    ):
        replacement_enrichment.append("ReplayGain")
    if replacement_enrichment:
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="replace_enrichment",
                severity="warning",
                title="Replaces enrichment values",
                message=f"The profile replaces existing {' and '.join(replacement_enrichment)} values when new values are available.",
            )
        )
    if profile.notification.refresh_external_servers:
        warnings.append(
            LibraryManagementProfileImportWarning(
                code="refresh_external_servers",
                severity="warning",
                title="Refreshes external media servers",
                message="Configured external media servers are refreshed after publication.",
            )
        )
    return warnings
