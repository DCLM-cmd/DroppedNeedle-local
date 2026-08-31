"""Jellyfin PascalCase response structs (reference/jellyfin-music-api.md)."""

from __future__ import annotations

import hashlib
from typing import Any

import msgspec

# Deterministic so it survives restarts without prefs; clients key persistent
# state by it (reference s2.3).
SERVER_ID = hashlib.sha256(b"droppedneedle-jellyfin-server").hexdigest()[:32]


class AuthenticateRequest(msgspec.Struct, kw_only=True):
    Username: str = ""
    Pw: str = ""


class PlaybackInfoBody(msgspec.Struct, kw_only=True):
    MaxStreamingBitrate: int | None = None
    StartTimeTicks: int | None = None
    UserId: str | None = None


class CreatePlaylistDto(msgspec.Struct, kw_only=True):
    Name: str = ""
    Ids: list[str] = []
    IsPublic: bool = False
    UserId: str | None = None


class PlaybackStartInfo(msgspec.Struct, kw_only=True):
    ItemId: str | None = None
    PlaySessionId: str | None = None


class PlaybackStopInfo(msgspec.Struct, kw_only=True):
    ItemId: str | None = None
    PlaySessionId: str | None = None
    PositionTicks: int | None = None
    RunTimeTicks: int | None = None
    Failed: bool = False


class PlaybackProgressInfo(msgspec.Struct, kw_only=True):
    ItemId: str | None = None
    PlaySessionId: str | None = None
    PositionTicks: int | None = None
    RunTimeTicks: int | None = None
    IsPaused: bool = False


class PublicSystemInfo(msgspec.Struct, kw_only=True):
    LocalAddress: str
    ServerName: str
    Version: str
    ProductName: str = "Jellyfin Server"
    OperatingSystem: str = ""
    Id: str = SERVER_ID
    StartupWizardCompleted: bool = True


class SystemInfo(msgspec.Struct, kw_only=True):
    LocalAddress: str
    ServerName: str
    Version: str
    ProductName: str = "Jellyfin Server"
    OperatingSystem: str = ""
    Id: str = SERVER_ID
    StartupWizardCompleted: bool = True
    HasPendingRestart: bool = False
    IsShuttingDown: bool = False
    SupportsLibraryMonitor: bool = True


class UserConfiguration(msgspec.Struct, kw_only=True):
    """Real Jellyfin sends this fully populated; strict clients hard-cast the
    bools, so an empty {} crashes them (Finamp: 'Null is not a subtype of bool',
    issue #144). Fields/defaults verified against jellyfin-sdk-typescript
    user-configuration.ts and Finamp lib/models/jellyfin_models.dart (its 9
    required fields are all non-optional here)."""

    AudioLanguagePreference: str | None = None
    PlayDefaultAudioTrack: bool = True
    SubtitleLanguagePreference: str | None = None
    DisplayMissingEpisodes: bool = False
    GroupedFolders: list[str] = []
    SubtitleMode: str = "Default"
    DisplayCollectionsView: bool = False
    EnableLocalPassword: bool = False
    OrderedViews: list[str] = []
    LatestItemsExcludes: list[str] = []
    MyMediaExcludes: list[str] = []
    HidePlayedInLatest: bool = True
    RememberAudioSelections: bool = True
    RememberSubtitleSelections: bool = True
    EnableNextEpisodeAutoPlay: bool = True


class SessionInfo(msgspec.Struct, kw_only=True):
    """Minimal session object for AuthenticationResult. Finamp requires UserId,
    LastActivityDate and the four bools non-null when SessionInfo is present
    (jellyfin_models.dart), so an empty {} crashes it (issue #144)."""

    Id: str
    UserId: str
    UserName: str
    LastActivityDate: str
    Client: str | None = None
    DeviceName: str = ""
    DeviceId: str | None = None
    IsActive: bool = True
    SupportsRemoteControl: bool = False
    SupportsMediaControl: bool = False
    HasCustomDeviceName: bool = False
    ServerId: str = SERVER_ID


class UserPolicy(msgspec.Struct, kw_only=True):
    """Real Jellyfin sends this fully populated, and strict clients hard-cast the
    bools - the same trap as UserConfiguration/SessionInfo (issue #144), which were
    typed for exactly this reason while Policy stayed a hand-written dict. Nine of
    Jellyfin's policy booleans were missing from that dict, and a missing key reaches
    Finamp as null: "type 'Null' is not a subtype of 'bool' in type cast" on login.

    Every boolean is non-optional here so it can never serialise as null.
    """

    IsAdministrator: bool = False
    IsHidden: bool = False
    IsDisabled: bool = False
    # Without EnableAllFolders strict clients (Manet) conclude "no libraries" and
    # never call /UserViews.
    EnableAllFolders: bool = True
    EnabledFolders: list[str] = []
    EnableAllChannels: bool = True
    EnabledChannels: list[str] = []
    EnableAllDevices: bool = True
    EnabledDevices: list[str] = []
    EnableMediaPlayback: bool = True
    EnableAudioPlaybackTranscoding: bool = True
    EnableVideoPlaybackTranscoding: bool = True
    EnablePlaybackRemuxing: bool = True
    ForceRemoteSourceTranscoding: bool = False
    EnableContentDownloading: bool = True
    EnableContentDeletion: bool = False
    EnableContentDeletionFromFolders: list[str] = []
    EnableRemoteAccess: bool = True
    EnableSyncTranscoding: bool = True
    EnableMediaConversion: bool = False
    EnableUserPreferenceAccess: bool = True
    EnableLiveTvAccess: bool = False
    EnableLiveTvManagement: bool = False
    EnableRemoteControlOfOtherUsers: bool = False
    EnableSharedDeviceControl: bool = False
    EnablePublicSharing: bool = False
    EnableCollectionManagement: bool = False
    EnableSubtitleManagement: bool = False
    EnableLyricManagement: bool = False
    EnableSubtitleDownloading: bool = False
    BlockedTags: list[str] = []
    AllowedTags: list[str] = []
    BlockedChannels: list[str] = []
    BlockedMediaFolders: list[str] = []
    BlockUnratedItems: list[str] = []
    AccessSchedules: list[dict[str, Any]] = []
    MaxParentalRating: int | None = None
    InvalidLoginAttemptCount: int = 0
    LoginAttemptsBeforeLockout: int = -1
    MaxActiveSessions: int = 0
    RemoteClientBitrateLimit: int = 0
    AuthenticationProviderId: str = (
        "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider"
    )
    PasswordResetProviderId: str = (
        "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider"
    )
    SyncPlayAccess: str = "CreateAndJoinGroups"


class UserDto(msgspec.Struct, kw_only=True):
    Id: str
    Name: str
    ServerId: str = SERVER_ID
    HasPassword: bool = True
    HasConfiguredPassword: bool = True
    # deprecated upstream but still sent by real servers; Finamp requires it non-null
    HasConfiguredEasyPassword: bool = False
    Configuration: UserConfiguration = msgspec.field(default_factory=UserConfiguration)
    Policy: UserPolicy = msgspec.field(default_factory=UserPolicy)


class AuthenticationResult(msgspec.Struct, kw_only=True):
    User: UserDto
    AccessToken: str
    SessionInfo: SessionInfo
    ServerId: str = SERVER_ID


class NameGuidPair(msgspec.Struct, kw_only=True):
    Name: str
    Id: str


class UserItemDataDto(msgspec.Struct, kw_only=True):
    ItemId: str
    Key: str
    PlaybackPositionTicks: int = 0
    PlayCount: int = 0
    IsFavorite: bool = False
    Played: bool = False
    LastPlayedDate: str | None = None
    Rating: float | None = None
    PlayedPercentage: float | None = None


class BaseItemPerson(msgspec.Struct, kw_only=True):
    """A credited person. For music Jellyfin lists the artists here, and clients read
    ``People`` when they want the credits behind a track rather than the flat
    ``Artists`` strings."""

    Name: str
    Id: str
    Role: str = ""
    Type: str = "Artist"
    PrimaryImageTag: str | None = None


class BaseItemDto(msgspec.Struct, kw_only=True):
    Id: str
    Name: str
    Type: str
    ServerId: str = SERVER_ID
    IsFolder: bool = False
    MediaType: str = "Unknown"
    RunTimeTicks: int | None = None
    ProductionYear: int | None = None
    IndexNumber: int | None = None         # track number
    ParentIndexNumber: int | None = None   # disc number
    Album: str | None = None
    AlbumId: str | None = None
    AlbumArtist: str | None = None
    AlbumArtists: list[NameGuidPair] | None = None
    ArtistItems: list[NameGuidPair] | None = None
    Artists: list[str] | None = None
    AlbumPrimaryImageTag: str | None = None
    ImageTags: dict[str, str] = {}
    ParentId: str | None = None
    Genres: list[str] | None = None
    Container: str | None = None
    ChildCount: int | None = None
    CollectionType: str | None = None
    SortName: str | None = None
    DateCreated: str | None = None
    ProviderIds: dict[str, str] | None = None
    UserData: UserItemDataDto | None = None
    PlaylistItemId: str | None = None      # only on playlist members
    # Real Jellyfin sets these non-null on every item (DtoService.AttachBasicFields);
    # strict clients (Manet, Swift Codable) require them, so default them (_strip_none
    # keeps "FileSystem"/{}/[]).
    LocationType: str = "FileSystem"
    BackdropImageTags: list[str] = []
    ImageBlurHashes: dict[str, dict[str, str]] = {}

    # --- fields clients ask for by name -----------------------------------------
    # Finamp's every listing carries
    # Fields=ChildCount,DateCreated,DateLastMediaAdded,Etag,Genres,ParentId,
    #        ProviderIds,Tags,SortName,People,MediaSources
    # and five of those were simply never emitted. A field a client ASKED for and did
    # not get is not a neutral omission: it silently disables the feature behind it.
    Etag: str | None = None
    Tags: list[str] = []
    People: list[BaseItemPerson] = []
    DateLastMediaAdded: str | None = None
    # Attaching the media source to the item saves a PlaybackInfo round-trip PER TRACK
    # when a client asks for it, which is most of the request storm behind "Finamp is
    # slow to load". Only populated when Fields names it - it is the expensive part.
    MediaSources: list["MediaSourceInfo"] | None = None
    MediaStreams: list["MediaStream"] | None = None
    MediaSourceCount: int | None = None

    # --- music metadata we hold and never published -------------------------------
    # ReplayGain. Clients use these to level playback across albums; without them
    # every track plays at whatever level it was mastered at.
    NormalizationGain: float | None = None
    AlbumNormalizationGain: float | None = None
    # Genres as navigable items, not just names - this is what a client links to when
    # you tap a genre.
    GenreItems: list[NameGuidPair] | None = None
    PremiereDate: str | None = None
    AlbumCount: int | None = None
    SongCount: int | None = None
    ArtistCount: int | None = None
    RecursiveItemCount: int | None = None
    CumulativeRunTimeTicks: int | None = None
    HasLyrics: bool | None = None
    Overview: str | None = None

    # --- capability flags a client gates its UI on --------------------------------
    # CanDownload decides whether a client offers offline downloads at all.
    CanDownload: bool = True
    CanDelete: bool = False
    PlayAccess: str = "Full"


class BaseItemDtoQueryResult(msgspec.Struct, kw_only=True):
    Items: list[BaseItemDto] = []
    TotalRecordCount: int = 0
    StartIndex: int = 0


class MediaStream(msgspec.Struct, kw_only=True):
    """A stream inside a media source, in Jellyfin's shape.

    The booleans below are NOT optional decoration. Clients generate their model from
    Jellyfin's OpenAPI schema, where these are non-nullable, and hard-cast them - the
    same trap that broke login twice (issue #144's UserConfiguration/SessionInfo, then
    UserPolicy). Fields whose value is None are dropped from the response entirely, so
    every one of these carries a real default rather than None.
    """

    Type: str = "Audio"
    Codec: str | None = None
    Index: int = 0
    BitRate: int | None = None
    Channels: int = 2
    ChannelLayout: str = "stereo"
    SampleRate: int | None = None
    BitDepth: int | None = None
    IsDefault: bool = True
    IsInterlaced: bool = False
    IsForced: bool = False
    IsExternal: bool = False
    IsTextSubtitleStream: bool = False
    SupportsExternalStream: bool = False
    IsHearingImpaired: bool = False
    Level: float = 0.0


class MediaSourceInfo(msgspec.Struct, kw_only=True):
    """One playable source for an item, in Jellyfin's shape.

    Finamp parses this with a generated ``_$MediaSourceInfoFromJson``, and a missing
    non-nullable field throws INSIDE the parser - so the item's whole metadata fetch
    fails ("Failed to fetch metadata for 'SKIT'") even though the HTTP call was a 200.
    Only a handful of these fields were emitted, so every track Finamp opened failed
    to parse.

    Fields set to None are dropped from the response, so anything a client may
    hard-cast has to carry a real default here, not None. Same rule as
    ``MediaStream``, ``UserPolicy`` and issue #144's structs.
    """

    Id: str
    Protocol: str = "File"
    Type: str = "Default"
    Container: str | None = None
    Size: int | None = None
    Bitrate: int | None = None
    RunTimeTicks: int | None = None
    SupportsDirectPlay: bool = True
    SupportsDirectStream: bool = True
    SupportsTranscoding: bool = False
    DefaultAudioStreamIndex: int = 0
    MediaStreams: list[MediaStream] = []
    MediaAttachments: list[dict] = []
    Formats: list[str] = []
    RequiredHttpHeaders: dict[str, str] = {}
    Name: str = ""
    ETag: str = ""
    IsRemote: bool = False
    ReadAtNativeFramerate: bool = False
    IgnoreDts: bool = False
    IgnoreIndex: bool = False
    GenPtsInput: bool = False
    IsInfiniteStream: bool = False
    RequiresOpening: bool = False
    RequiresClosing: bool = False
    RequiresLooping: bool = False
    SupportsProbing: bool = True
    HasSegments: bool = False
    # api_key must be embedded in the URL: clients (Finamp, Jellify, Manet) fetch it
    # without auth headers, so the stream 401s otherwise.
    DirectStreamUrl: str | None = None
    TranscodingUrl: str | None = None
    TranscodingSubProtocol: str | None = None
    TranscodingContainer: str | None = None


class PlaybackInfoResponse(msgspec.Struct, kw_only=True):
    MediaSources: list[MediaSourceInfo] = []
    PlaySessionId: str
    ErrorCode: str | None = None
