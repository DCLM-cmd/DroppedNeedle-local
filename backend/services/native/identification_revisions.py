"""Stable revision inputs shared by identification and coverage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def _digest(values: list[str]) -> str:
    return hashlib.sha256("|".join(values).encode()).hexdigest()


def album_input_revisions(tracks: list[dict]) -> tuple[str, str, str]:
    ordered = sorted(tracks, key=lambda track: str(track["id"]))
    return (
        _digest([f"{track['id']}:{track['tag_revision'] or ''}" for track in ordered]),
        _digest([f"{track['id']}:{track['stat_revision']}" for track in ordered]),
        _digest(
            [
                f"{track['id']}:{track['applied_policy_revision']}:{track['applied_policy']}"
                for track in ordered
            ]
        ),
    )


def album_identity_revision(
    album_identity: Mapping[str, object] | None,
    tracks: list[Mapping[str, object]],
) -> str:
    """Seal the provider identities used while evaluating an indexed album."""

    album_payload = (
        None
        if album_identity is None
        else {
            "row_revision": album_identity.get("row_revision"),
            "release_group_mbid": album_identity.get("release_group_mbid"),
            "release_mbid": album_identity.get("release_mbid"),
            "decision_source": album_identity.get("decision_source"),
        }
    )
    track_payload = [
        {
            "id": track.get("id"),
            "identity_row_revision": track.get("identity_row_revision"),
            "recording_mbid": track.get("recording_mbid"),
            "release_mbid": track.get("identity_release_mbid"),
            "release_track_mbid": track.get("release_track_mbid"),
            "medium_position": track.get("medium_position"),
            "release_track_position": track.get("release_track_position"),
        }
        for track in sorted(tracks, key=lambda track: str(track.get("id")))
    ]
    return hashlib.sha256(
        json.dumps(
            {"album": album_payload, "tracks": track_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
