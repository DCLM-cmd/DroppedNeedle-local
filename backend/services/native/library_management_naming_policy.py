"""Pure dynamic naming selection for Library Management paths."""

import re

from api.v1.schemas.library_management import (
    LibraryManagementRootAssignment,
    NamingScriptSettings,
)
from core.exceptions import ValidationError
from models.library_management_canonical import (
    CanonicalReleaseDocument,
    CanonicalTrackDocument,
)
from models.library_management_planning import (
    ManagementNamingContext,
    PinnedLibraryManagementProfile,
    naming_policy_revision,
)


def select_naming_script(
    pinned: PinnedLibraryManagementProfile,
    organization_audio_medium_count: int,
) -> NamingScriptSettings:
    if (
        organization_audio_medium_count > 1
        and pinned.multi_disc_naming_script is not None
    ):
        return pinned.multi_disc_naming_script
    return pinned.naming_script


# MusicBrainz qualifies a medium format by its physical size - ``12" Vinyl``,
# ``7" Vinyl``, ``10" Shellac``. Media scanners recognise a disc SUBFOLDER only when
# the format word comes first (Jellyfin matches a leading cd/disc/disk/dvd/vinyl/
# digital media, Plex and Navidrome are equivalent), so ``12" Vinyl 01`` read as an
# album in its own right and every vinyl release showed up split into one album per
# side. Dropping the size qualifier yields ``Vinyl 01``, which they all recognise -
# and the size was never information the folder name needed to carry, since the
# tracks keep their own tags.
_SIZE_QUALIFIER = re.compile(r'^\s*\d+(?:\.\d+)?\s*(?:"|\u2033|in\b|inch(?:es)?\b)\s*')


def normalise_medium_format(value: str) -> str:
    """A medium format a media scanner can recognise at the head of a folder name."""
    return _SIZE_QUALIFIER.sub("", (value or "").strip()).strip()


def management_naming_context(
    release: CanonicalReleaseDocument,
    track: CanonicalTrackDocument,
) -> ManagementNamingContext:
    if track.disc_number < 1:
        raise ValidationError(
            "A mapped MusicBrainz track needs a valid medium position for naming."
        )
    return ManagementNamingContext(
        album_disambiguation=release.album_disambiguation,
        medium_format=normalise_medium_format(track.media_format or ""),
        medium_number=track.disc_number,
        organization_audio_medium_count=release.organization_audio_medium_count,
    )


def activation_naming_policy_matches(
    assignment: LibraryManagementRootAssignment,
    pinned: PinnedLibraryManagementProfile,
) -> bool:
    expected = naming_policy_revision(pinned)
    if assignment.activation_naming_policy_revision is not None:
        return assignment.activation_naming_policy_revision == expected
    return pinned.multi_disc_naming_script is None
