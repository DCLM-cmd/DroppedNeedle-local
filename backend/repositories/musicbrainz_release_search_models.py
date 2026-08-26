"""Typed MusicBrainz release-search payloads.

Live response verified against MusicBrainz ``/ws/2/release`` on 2026-08-11.
The search index includes release-group, artist-credit, label-info and medium
facets without an ``inc`` parameter; optional fields are absent on many releases.
"""

import msgspec


class MbReleaseSearchArtist(msgspec.Struct):
    id: str = ""
    name: str = ""


class MbReleaseSearchArtistCredit(msgspec.Struct):
    name: str = ""
    artist: MbReleaseSearchArtist = msgspec.field(default_factory=MbReleaseSearchArtist)
    joinphrase: str = ""


class MbReleaseSearchGroup(msgspec.Struct):
    id: str = ""
    title: str = ""


class MbReleaseSearchLabel(msgspec.Struct):
    id: str = ""
    name: str = ""


class MbReleaseSearchLabelInfo(msgspec.Struct):
    catalog_number: str | None = msgspec.field(name="catalog-number", default=None)
    label: MbReleaseSearchLabel | None = None


class MbReleaseSearchMedium(msgspec.Struct):
    format: str | None = None
    track_count: int = msgspec.field(name="track-count", default=0)


class MbReleaseSearchRelease(msgspec.Struct):
    id: str = ""
    score: int = 0
    title: str = ""
    artist_credit: list[MbReleaseSearchArtistCredit] = msgspec.field(
        name="artist-credit", default_factory=list
    )
    release_group: MbReleaseSearchGroup = msgspec.field(
        name="release-group", default_factory=MbReleaseSearchGroup
    )
    date: str | None = None
    country: str | None = None
    status: str | None = None
    packaging: str | None = None
    media: list[MbReleaseSearchMedium] = msgspec.field(default_factory=list)
    label_info: list[MbReleaseSearchLabelInfo] = msgspec.field(
        name="label-info", default_factory=list
    )
    barcode: str | None = None
    disambiguation: str | None = None


class MbReleaseSearchResponse(msgspec.Struct):
    count: int = 0
    offset: int = 0
    releases: list[MbReleaseSearchRelease] = msgspec.field(default_factory=list)
