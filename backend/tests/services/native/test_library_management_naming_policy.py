import msgspec

from api.v1.schemas.library_management import (
    NamingScriptSettings,
    build_initial_library_management_settings,
)
from models.library_management_canonical import (
    CanonicalArtistCredit,
    CanonicalDate,
    CanonicalIdentifierSet,
    CanonicalReleaseDocument,
    CanonicalTrackDocument,
)
from models.library_management_planning import (
    PinnedLibraryManagementProfile,
    naming_policy_revision,
)
from services.native.library_management_naming_policy import (
    management_naming_context,
    select_naming_script,
)


def _script(identifier: str, revision: str) -> NamingScriptSettings:
    return NamingScriptSettings(
        id=identifier,
        name=identifier,
        source="{title}.{ext}",
        revision=revision,
    )


def _pinned(*, multi: bool = True) -> PinnedLibraryManagementProfile:
    profile = build_initial_library_management_settings().profiles[0]
    return PinnedLibraryManagementProfile(
        profile=profile,
        naming_script=_script("00000000-0000-4000-8000-000000000001", "standard"),
        multi_disc_naming_script=(
            _script("00000000-0000-4000-8000-000000000002", "multi") if multi else None
        ),
    )


def test_selector_uses_multi_only_for_more_than_one_audio_medium() -> None:
    pinned = _pinned()

    assert select_naming_script(pinned, 1).revision == "standard"
    assert select_naming_script(pinned, 2).revision == "multi"
    assert select_naming_script(pinned, 3).revision == "multi"
    assert select_naming_script(_pinned(multi=False), 2).revision == "standard"


def test_standard_only_revision_is_exactly_legacy_script_revision() -> None:
    assert naming_policy_revision(_pinned(multi=False)) == "standard"
    assert naming_policy_revision(_pinned()) != "standard"


def test_old_single_script_pinned_profile_json_decodes_unchanged() -> None:
    payload = msgspec.to_builtins(_pinned(multi=False))
    payload.pop("multi_disc_naming_script")

    decoded = msgspec.json.decode(
        msgspec.json.encode(payload), type=PinnedLibraryManagementProfile
    )

    assert decoded.multi_disc_naming_script is None
    assert naming_policy_revision(decoded) == "standard"


def test_context_uses_canonical_release_and_medium_values() -> None:
    artist = CanonicalArtistCredit(
        display_name="Artist",
        credited_name="Artist",
        canonical_name="Artist",
        sort_name="Artist",
        artist_mbid="artist",
    )
    identifiers = CanonicalIdentifierSet(
        release_group_mbid="group", release_mbid="release"
    )
    release = CanonicalReleaseDocument(
        local_album_id="album",
        source_album_revision=1,
        source_identity_revision=1,
        title="Album",
        artist_credits=(artist,),
        identifiers=identifiers,
        date=CanonicalDate(value="2026", precision="year"),
        original_date=None,
        release_status=None,
        release_country=None,
        primary_release_type=None,
        secondary_release_types=(),
        packaging=None,
        barcode=None,
        asin=None,
        language=None,
        script=None,
        compilation=False,
        total_discs=3,
        labels=(),
        genres=(),
        media=(),
        album_disambiguation="Deluxe Edition",
        organization_audio_medium_count=2,
    )
    track = CanonicalTrackDocument(
        local_track_id="track",
        source_track_revision=1,
        source_identity_revision=1,
        title="Track",
        artist_credits=(artist,),
        relationship_credits=(),
        identifiers=identifiers,
        track_number=1,
        track_number_text="1",
        total_tracks=1,
        disc_number=3,
        total_discs=3,
        media_format="CD",
    )

    context = management_naming_context(release, track)

    assert context.album_disambiguation == "Deluxe Edition"
    assert context.medium_format == "CD"
    assert context.medium_number == 3
    assert context.organization_audio_medium_count == 2
