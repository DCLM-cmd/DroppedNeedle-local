"""View DTO -> Jellyfin BaseItemDto builder (06-data-mapping.md s5)."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING

from api.compat.jellyfin.models import (
    BaseItemDto,
    BaseItemPerson,
    NameGuidPair,
    UserItemDataDto,
)
from infrastructure.constants import JELLYFIN_TICKS_PER_SECOND

if TYPE_CHECKING:
    from repositories.coverart_repository import CoverArtRepository
    from services.compat.id_map_service import CompatIdMapService
    from services.compat.view_models import (
        ViewAlbum,
        ViewArtist,
        ViewGenre,
        ViewPlaylist,
        ViewTrack,
    )

logger = logging.getLogger(__name__)

LIBRARY_INTERNAL_ID = "music"


def ticks(seconds: float | None) -> int | None:
    if seconds is None:
        return None
    return round(seconds * JELLYFIN_TICKS_PER_SECOND)


def genre_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


# Manet requires DateCreated present, so never emit null even with no library date.
_DEFAULT_DATE = "1970-01-01T00:00:00.0000000Z"


def _iso(ts: float | int | None) -> str | None:
    if ts is None:
        return None
    from datetime import datetime, timezone

    # .NET "O" round-trip format: strict clients (Manet) reject whole-second ISO,
    # the 7-digit fraction is required.
    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond:06d}0Z"


class JellyfinBuilder:
    def __init__(
        self,
        id_map: "CompatIdMapService",
        coverart: "CoverArtRepository",
        server_id: str,
        fields: "set[str] | None" = None,
    ) -> None:
        self._ids = id_map
        self._cover = coverart
        self._sid = server_id
        # Tag and blurhash come from one image record, so they are cached as a pair.
        self._aspect_ratio_cache: dict[str, float | None] = {}
        self._album_image_cache: dict[str, tuple[str | None, str | None]] = {}
        # The client's ``Fields=`` list, lowercased. Jellyfin attaches the expensive
        # extras only when they are named, and so do we.
        self._fields = {f.strip().casefold() for f in (fields or set()) if f.strip()}
        self._wants_sources = "mediasources" in self._fields

    async def _album_image(self, rg_mbid: str | None) -> tuple[str | None, str | None]:
        """The album cover's tag and blurhash, resolved once and cached per request.

        Fetching them separately made every listed album resolve the same picture
        twice - two catalog lookups and two identity reads apiece, 358 ms of a 378 ms
        hundred-album page. Jellyfin takes both off one stored image record.
        """
        if not rg_mbid:
            return None, None
        if rg_mbid not in self._album_image_cache:
            info = await self._art_call("get_release_group_cover_image_info", rg_mbid)
            if isinstance(info, tuple) and len(info) == 2:
                tag, blurhash = info
            else:
                tag = blurhash = None
            self._album_image_cache[rg_mbid] = (
                tag if isinstance(tag, str) and tag else None,
                blurhash if isinstance(blurhash, str) and blurhash else None,
            )
        return self._album_image_cache[rg_mbid]

    async def _album_aspect_ratio(self, rg_mbid: str | None) -> float | None:
        """The cover's aspect ratio, off the same stored record as its blurhash."""
        tag, _ = await self._album_image(rg_mbid)
        if not tag:
            return None
        if tag not in self._aspect_ratio_cache:
            self._aspect_ratio_cache[tag] = await self._art_call(
                "get_image_aspect_ratio", tag
            )
        value = self._aspect_ratio_cache[tag]
        return value if isinstance(value, (int, float)) else None

    async def _album_tag(self, rg_mbid: str | None) -> str | None:
        return (await self._album_image(rg_mbid))[0]

    async def _art_call(self, method: str, *args):
        """Call an optional cover-repository method, tolerating art being unavailable.

        A blanket ``except Exception`` here previously hid an AttributeError: the
        target composition wraps the cover repository in an adapter, the adapter had
        no blurhash method, and every album silently reported none. A missing METHOD
        is a wiring bug and is logged as one; a failing LOOKUP is just art being
        unavailable and stays quiet.
        """
        function = getattr(self._cover, method, None)
        if function is None:
            logger.warning(
                "Cover repository %s does not implement %s; the compat API will omit "
                "that field",
                type(self._cover).__name__,
                method,
            )
            return None
        try:
            return await function(*args)
        except Exception:  # noqa: BLE001 - art is best-effort
            logger.debug("%s failed for %s", method, args, exc_info=True)
            return None

    async def _album_blurhash(self, rg_mbid: str | None) -> str | None:
        return (await self._album_image(rg_mbid))[1]

    async def _artist_image(
        self, artist_mbid: str | None
    ) -> tuple[str | None, str | None]:
        """The artist picture's tag and blurhash. See ``_album_image``."""
        if not artist_mbid:
            return None, None
        info = await self._art_call("get_artist_image_info", artist_mbid)
        if not isinstance(info, tuple) or len(info) != 2:
            return None, None
        tag, blurhash = info
        return (
            tag if isinstance(tag, str) and tag else None,
            blurhash if isinstance(blurhash, str) and blurhash else None,
        )

    async def _artist_blurhash(self, artist_mbid: str | None) -> str | None:
        return (await self._artist_image(artist_mbid))[1]

    @staticmethod
    def _blur_hashes(tag: object, blurhash: object) -> dict:
        """Jellyfin's ``ImageBlurHashes`` shape: ``{"Primary": {<tag>: <hash>}}``.

        Clients key the hash by the image tag, which is how they tell that an image
        they already hold is the same one - that is the de-duplication Finamp warns
        about when a server publishes nothing here.

        Both halves must be actual strings. The field is typed ``dict[str, dict[str,
        str]]``, so anything else is a contract violation that only surfaces at the
        very end of the request, in the encoder, as a 500 on the whole listing - and
        the cover repository is reached through an adapter, so what arrives here is
        not always what this module's type hints promise.
        """
        if isinstance(tag, str) and isinstance(blurhash, str) and tag and blurhash:
            return {"Primary": {tag: blurhash}}
        return {}

    async def _artist_tag(self, artist_mbid: str | None) -> str | None:
        """The artist's image tag - only when there is actually an image.

        This used to fall back to a stand-in tag derived from the artist id, to make
        clients ask for pictures that were fetched on demand and so break the deadlock
        where nothing was cached because nothing requested it. The organization run
        now materialises artist art up front, the way Jellyfin downloads remote images
        while scanning, so the stand-in has become purely harmful: it advertised
        pictures that did not exist, the image route answered 404, and Finamp - which
        reads a tag without a matching blurhash as a broken server - warned the user
        that the server "does not compute blurhashes" while showing a placeholder.

        An artist with no cached image now simply reports no image, which is what
        Jellyfin does and what clients handle gracefully.
        """
        if not artist_mbid:
            return None
        cached = await self._art_call("get_artist_image_etag", artist_mbid)
        return cached if isinstance(cached, str) and cached else None

    @staticmethod
    def _etag(*parts) -> str:
        """A stable per-item entity tag.

        Jellyfin publishes one and clients ask for it by name (``Fields=Etag``) to
        revalidate what they have cached. It only has to change when the item does, so
        it is derived from the identity plus the values a client renders.
        """
        material = "\x1f".join("" if p is None else str(p) for p in parts)
        return hashlib.sha1(material.encode()).hexdigest()

    async def _genre_items(self, genre: str | None) -> list[NameGuidPair]:
        """Genres as navigable items. ``Genres`` carries the names; ``GenreItems``
        carries the ids a client needs to actually open one."""
        if not genre:
            return []
        return [
            NameGuidPair(
                Name=genre, Id=await self._ids.to_jf("genre", genre_slug(genre))
            )
        ]

    @staticmethod
    def _people(pairs: list[NameGuidPair]) -> list[BaseItemPerson]:
        """The credits behind an item. Finamp requests ``Fields=People`` on every
        listing; Jellyfin answers with the artists for music items."""
        return [
            BaseItemPerson(Name=pair.Name, Id=pair.Id, Type="Artist", Role="Artist")
            for pair in pairs
            if pair.Name and pair.Id
        ]

    @staticmethod
    def _user_data(item_id: str, *, starred_at, play_count) -> UserItemDataDto:
        count = play_count or 0
        return UserItemDataDto(
            ItemId=item_id,
            Key=item_id,
            PlayCount=count,
            Played=count > 0,
            IsFavorite=starred_at is not None,
        )

    def _media_source(self, t: "ViewTrack", item_id: str) -> "MediaSourceInfo":
        """The playable source for a track, in the shape /PlaybackInfo returns.

        Attached to the item itself when the client asked for ``Fields=MediaSources``
        - Finamp asks on every listing, and getting it here spares one PlaybackInfo
        round-trip PER TRACK. No stream URL: this is metadata about the source, and a
        URL would have to carry the caller's token, which a cached listing must not.
        """
        from api.compat.jellyfin.models import MediaSourceInfo

        return MediaSourceInfo(
            Id=item_id,
            Container=t.file_format or None,
            Size=t.file_size_bytes or None,
            Bitrate=(t.bitrate or 0) * 1000 or None,
            RunTimeTicks=ticks(t.duration_seconds),
            Name=t.title,
            DefaultAudioStreamIndex=0,
            MediaStreams=[self._media_stream(t)],
        )

    @staticmethod
    def _media_stream(t: "ViewTrack") -> "MediaStream":
        from api.compat.jellyfin.models import MediaStream

        return MediaStream(
            Type="Audio",
            Codec=t.file_format or None,
            Index=0,
            BitRate=(t.bitrate or 0) * 1000 or None,
            Channels=t.channels or 2,
            ChannelLayout="stereo" if (t.channels or 2) == 2 else "mono",
            SampleRate=t.sample_rate,
            BitDepth=t.bit_depth,
        )

    async def audio(self, t: "ViewTrack") -> BaseItemDto:
        track_id = await self._ids.to_jf("track", t.file_id)
        album_id = await self._ids.to_jf("album", t.rg_mbid) if t.rg_mbid else None
        artist_mbid = t.artist_mbid
        album_artist_mbid = t.album_artist_mbid or t.artist_mbid
        artist_jf = (
            await self._ids.to_jf("artist", artist_mbid) if artist_mbid else None
        )
        album_artist_jf = (
            await self._ids.to_jf("artist", album_artist_mbid)
            if album_artist_mbid
            else None
        )
        album_tag = await self._album_tag(t.rg_mbid)
        recording_mbid = t.musicbrainz_recording_id or (
            t.recording_mbid if not t.provider_identity_projected else None
        )
        provider_ids = {"MusicBrainzTrack": recording_mbid} if recording_mbid else None
        return BaseItemDto(
            Id=track_id,
            Name=t.title,
            ServerId=self._sid,
            Type="Audio",
            IsFolder=False,
            MediaType="Audio",
            SortName=t.title,
            RunTimeTicks=ticks(t.duration_seconds),
            ProductionYear=t.year,
            IndexNumber=t.track_number or None,
            ParentIndexNumber=t.disc_number or None,
            Album=t.album_title,
            AlbumId=album_id,
            AlbumArtist=t.album_artist_name or t.artist_name,
            # Jellyfin emits these as a (possibly empty) array, never null; strict
            # clients (Manet) require them present.
            AlbumArtists=[
                NameGuidPair(
                    Name=t.album_artist_name or t.artist_name, Id=album_artist_jf
                )
            ]
            if album_artist_jf
            else [],
            ArtistItems=[NameGuidPair(Name=t.artist_name, Id=artist_jf)]
            if artist_jf
            else [],
            Artists=[t.artist_name] if t.artist_name else [],
            AlbumPrimaryImageTag=album_tag,
            ImageTags={"Primary": album_tag} if album_tag else {},
            ImageBlurHashes=self._blur_hashes(
                album_tag, await self._album_blurhash(t.rg_mbid)
            ),
            PrimaryImageAspectRatio=await self._album_aspect_ratio(t.rg_mbid),
            ParentId=album_id,
            Container=t.file_format or None,
            Genres=[t.genre] if t.genre else [],
            GenreItems=await self._genre_items(t.genre),
            People=self._people(
                [NameGuidPair(Name=t.artist_name, Id=artist_jf)] if artist_jf else []
            ),
            Etag=self._etag(
                track_id, t.title, t.album_title, t.artist_name, t.duration_seconds,
                t.file_size_bytes, t.file_format, t.bitrate,
            ),
            Tags=[],
            PremiereDate=t.original_release_date or None,
            # ReplayGain: without these a client cannot level playback across albums.
            NormalizationGain=t.replaygain_track_gain,
            AlbumNormalizationGain=t.replaygain_album_gain,
            MediaSources=[self._media_source(t, track_id)] if self._wants_sources else None,
            MediaStreams=[self._media_stream(t)] if self._wants_sources else None,
            MediaSourceCount=1 if self._wants_sources else None,
            ProviderIds=provider_ids,
            DateCreated=_iso(t.created_at) or _DEFAULT_DATE,
            UserData=self._user_data(
                track_id, starred_at=t.starred_at, play_count=t.play_count
            ),
        )

    async def album(self, a: "ViewAlbum") -> BaseItemDto:
        album_id = await self._ids.to_jf("album", a.rg_mbid)
        artist_jf = (
            await self._ids.to_jf("artist", a.artist_mbid) if a.artist_mbid else None
        )
        tag = await self._album_tag(a.rg_mbid)
        return BaseItemDto(
            Id=album_id,
            Name=a.title,
            ServerId=self._sid,
            Type="MusicAlbum",
            IsFolder=True,
            MediaType="Unknown",
            SortName=a.title,
            RunTimeTicks=ticks(a.total_duration_seconds),
            ProductionYear=a.year,
            ChildCount=a.track_count,
            AlbumArtist=a.artist_name,
            AlbumArtists=[NameGuidPair(Name=a.artist_name, Id=artist_jf)]
            if (artist_jf and a.artist_name)
            else [],
            ArtistItems=[NameGuidPair(Name=a.artist_name, Id=artist_jf)]
            if (artist_jf and a.artist_name)
            else [],
            Artists=[a.artist_name] if a.artist_name else [],
            Genres=[a.genre] if a.genre else [],
            ImageTags={"Primary": tag} if tag else {},
            ImageBlurHashes=self._blur_hashes(tag, await self._album_blurhash(a.rg_mbid)),
            PrimaryImageAspectRatio=await self._album_aspect_ratio(a.rg_mbid),
            GenreItems=await self._genre_items(a.genre),
            People=self._people(
                [NameGuidPair(Name=a.artist_name, Id=artist_jf)]
                if (artist_jf and a.artist_name)
                else []
            ),
            Etag=self._etag(
                album_id, a.title, a.artist_name, a.year, a.track_count,
                a.total_duration_seconds,
            ),
            Tags=[],
            PremiereDate=a.original_release_date or None,
            DateLastMediaAdded=_iso(a.date_added),
            SongCount=a.track_count,
            RecursiveItemCount=a.track_count,
            CumulativeRunTimeTicks=ticks(a.total_duration_seconds),
            ProviderIds=(
                {"MusicBrainzReleaseGroup": a.musicbrainz_release_group_id or a.rg_mbid}
                if a.musicbrainz_release_group_id or not a.provider_identity_projected
                else None
            ),
            DateCreated=_iso(a.date_added) or _DEFAULT_DATE,
            UserData=self._user_data(
                album_id, starred_at=a.starred_at, play_count=a.play_count
            ),
        )

    async def artist(self, ar: "ViewArtist") -> BaseItemDto:
        artist_id = await self._ids.to_jf("artist", ar.artist_mbid)
        tag, blurhash = await self._artist_image(ar.artist_mbid)
        if not blurhash:
            # An artist picture is fetched on demand, so one can enter the cache -
            # and gain a tag - between two organization runs, before its hash exists.
            # Finamp treats a tag whose hash is missing as a misconfigured server and
            # tells the user so. Withholding the tag until the pair is complete keeps
            # that impossible; the next run stores the hash and the picture appears.
            tag = None
        return BaseItemDto(
            Id=artist_id,
            Name=ar.name,
            ServerId=self._sid,
            Type="MusicArtist",
            IsFolder=True,
            MediaType="Unknown",
            ChildCount=ar.album_count,
            ImageTags={"Primary": tag} if tag else {},
            ImageBlurHashes=self._blur_hashes(tag, blurhash),
            Etag=self._etag(artist_id, ar.name, ar.album_count),
            Tags=[],
            # Only what the view actually knows: an artist's song count and total
            # runtime would each cost a separate aggregate per row, which is not worth
            # a listing's latency.
            AlbumCount=ar.album_count,
            RecursiveItemCount=ar.album_count,
            DateLastMediaAdded=_iso(ar.date_added),
            ProviderIds=(
                {"MusicBrainzArtist": ar.musicbrainz_artist_id or ar.artist_mbid}
                if ar.musicbrainz_artist_id or not ar.provider_identity_projected
                else None
            ),
            SortName=ar.name,
            Genres=[],
            DateCreated=_iso(ar.date_added) or _DEFAULT_DATE,
            UserData=self._user_data(
                artist_id, starred_at=ar.starred_at, play_count=None
            ),
        )

    async def playlist(self, p: "ViewPlaylist") -> BaseItemDto:
        pid = await self._ids.to_jf("playlist", p.id)
        return BaseItemDto(
            Id=pid,
            Name=p.name,
            ServerId=self._sid,
            Type="Playlist",
            IsFolder=True,
            MediaType="Audio",
            ChildCount=p.track_count,
            SortName=p.name,
            RunTimeTicks=ticks(p.total_duration_seconds),
            UserData=self._user_data(pid, starred_at=None, play_count=None),
        )

    async def genre(self, g: "ViewGenre") -> BaseItemDto:
        gid = await self._ids.to_jf("genre", genre_slug(g.name))
        return BaseItemDto(
            Id=gid,
            Name=g.name,
            ServerId=self._sid,
            Type="MusicGenre",
            IsFolder=True,
            MediaType="Unknown",
            ChildCount=g.song_count,
            SortName=g.name,
            UserData=self._user_data(gid, starred_at=None, play_count=None),
        )
