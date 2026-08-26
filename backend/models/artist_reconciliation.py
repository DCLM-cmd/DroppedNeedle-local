"""Provider-proven artist-credit projection contracts."""

from __future__ import annotations

import msgspec


class ProviderArtistCredit(msgspec.Struct, frozen=True, kw_only=True):
    position: int
    artist_mbid: str
    canonical_name: str
    credited_name: str
    sort_name: str = ""
    join_phrase: str = ""


class ProviderTrackArtistProjection(msgspec.Struct, frozen=True, kw_only=True):
    local_track_id: str
    track_revision: int
    release_track_mbid: str
    track_identity_revision: int
    credits: tuple[ProviderArtistCredit, ...]


class ProviderAlbumArtistProjection(msgspec.Struct, frozen=True, kw_only=True):
    local_album_id: str
    album_revision: int
    release_mbid: str
    album_identity_revision: int
    input_revision: str
    evidence_hash: str
    album_credits: tuple[ProviderArtistCredit, ...]
    tracks: tuple[ProviderTrackArtistProjection, ...]
    incomplete_track_mapping: bool = False
