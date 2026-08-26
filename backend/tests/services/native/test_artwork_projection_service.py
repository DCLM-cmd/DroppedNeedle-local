from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from api.v1.schemas.library_management import (
    ArtworkManagementSettings,
    complete_library_organizer_profile,
)
from core.exceptions import ExternalServiceError
from infrastructure.audio.artwork_processor import ArtworkProcessor
from infrastructure.queue.priority_queue import RequestPriority
from models.audio_metadata import EmbeddedArtworkDescriptor
from models.library_management_artwork import (
    ArtworkCandidate,
    ArtworkOutput,
    ExistingArtworkDescriptor,
)
from services.native.artwork_projection_service import (
    ArtworkProjectionService,
    desired_embedded_artwork,
)

_RELEASE = "aff0622e-7bd3-4fb6-9ca3-0fa19dd2340b"
_RG = "dcff25f1-702d-3b5e-b0da-d48172e6e62a"


def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="PNG")
    return output.getvalue()


def _candidate(
    candidate_id: str,
    *,
    source: str,
    image_type: str = "front",
    approved: bool = True,
) -> ArtworkCandidate:
    return ArtworkCandidate(
        candidate_id=candidate_id,
        source=source,
        locator=f"https://coverartarchive.org/{candidate_id}.png",
        image_types=(image_type,),
        approved=approved,
        primary=image_type == "front",
        source_is_exact_release=source == "cover_art_archive_release",
    )


def _existing(
    *, image_type: str = "front", width: int | None = 50, height: int | None = 50
) -> ExistingArtworkDescriptor:
    return ExistingArtworkDescriptor(
        image_type=image_type,
        mime_type="image/png",
        width=width,
        height=height,
        byte_size=100,
        sha256="existing",
    )


class StubArtworkRepository:
    def __init__(self) -> None:
        self.candidates: dict[str, tuple[ArtworkCandidate, ...]] = {
            "release": (),
            "release-group": (),
        }
        self.content: dict[str, bytes] = {}
        self.fail: set[str] = set()
        self.calls: list[str] = []

    async def list_management_artwork(
        self,
        *,
        entity_kind: str,
        mbid: str,
        download_size: str,
        priority: RequestPriority,
    ) -> tuple[ArtworkCandidate, ...]:
        del mbid, download_size, priority
        self.calls.append(entity_kind)
        if entity_kind in self.fail:
            raise ExternalServiceError("provider unavailable")
        return self.candidates[entity_kind]

    async def download_management_artwork(
        self,
        candidate: ArtworkCandidate,
        *,
        maximum_bytes: int,
        priority: RequestPriority,
    ) -> tuple[bytes, str | None]:
        del priority
        content = self.content[candidate.candidate_id]
        if len(content) > maximum_bytes:
            raise ExternalServiceError("too large")
        return content, "image/png"


def test_merge_embedded_artwork_replaces_only_matching_image_types() -> None:
    front = EmbeddedArtworkDescriptor(
        image_type="front",
        mime_type="image/jpeg",
        description="old front",
        width=700,
        height=700,
        byte_size=9,
        sha256="old-front",
        content=b"old-front",
        format_supported=True,
    )
    back = EmbeddedArtworkDescriptor(
        image_type="back",
        mime_type="image/jpeg",
        description="back",
        width=600,
        height=600,
        byte_size=4,
        sha256="back",
        content=b"back",
        format_supported=True,
    )
    replacement = ArtworkOutput(
        output_kind="embedded",
        image_type="front",
        content=b"new-front",
        mime_type="image/jpeg",
        format="jpeg",
        width=1200,
        height=1200,
        byte_size=9,
        sha256="new-front",
        source="cover_art_archive_release",
        source_candidate_id="candidate",
        source_is_exact_release=True,
    )

    preserved = desired_embedded_artwork((front, back), ())
    replaced = desired_embedded_artwork((front, back), (replacement,))

    assert preserved == (front, back)
    assert [(value.image_type, value.sha256) for value in replaced] == [
        ("front", "new-front"),
        ("back", "back"),
    ]


@pytest.mark.asyncio
async def test_exact_release_wins_and_group_is_per_type_fallback() -> None:
    repository = StubArtworkRepository()
    exact = _candidate("exact-front", source="cover_art_archive_release")
    fallback = _candidate(
        "fallback-back",
        source="cover_art_archive_release_group",
        image_type="back",
    )
    repository.candidates["release"] = (exact,)
    repository.candidates["release-group"] = (fallback,)
    repository.content = {
        exact.candidate_id: _png(100, 100, (200, 20, 20)),
        fallback.candidate_id: _png(120, 100, (20, 20, 200)),
    }
    settings = ArtworkManagementSettings(
        image_types=["front", "back"],
        embedded_front_only=False,
        external_enabled=False,
    )

    projection = await ArtworkProjectionService(repository, ArtworkProcessor()).project(
        settings=settings,
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=None,
        existing_embedded=(),
        existing_external=(),
        priority=RequestPriority.USER_INITIATED,
    )

    assert [value.source for value in projection.embedded] == [
        "cover_art_archive_release",
        "cover_art_archive_release_group",
    ]
    assert [value.image_type for value in projection.embedded] == ["front", "back"]
    assert projection.embedded[0].source_is_exact_release is True
    assert projection.embedded[1].source_is_exact_release is False


@pytest.mark.asyncio
async def test_approval_type_and_minimum_dimensions_filter_candidates() -> None:
    repository = StubArtworkRepository()
    unapproved = _candidate(
        "unapproved", source="cover_art_archive_release", approved=False
    )
    too_small = _candidate("small", source="cover_art_archive_release")
    fallback = _candidate("fallback", source="cover_art_archive_release_group")
    repository.candidates["release"] = (unapproved, too_small)
    repository.candidates["release-group"] = (fallback,)
    repository.content = {
        unapproved.candidate_id: _png(400, 400, (1, 1, 1)),
        too_small.candidate_id: _png(40, 40, (2, 2, 2)),
        fallback.candidate_id: _png(200, 180, (3, 3, 3)),
    }
    settings = ArtworkManagementSettings(
        minimum_width=100,
        minimum_height=100,
        external_enabled=False,
    )

    projection = await ArtworkProjectionService(repository, ArtworkProcessor()).project(
        settings=settings,
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=None,
        existing_embedded=(),
        existing_external=(),
        priority=RequestPriority.BACKGROUND_SYNC,
    )

    assert len(projection.embedded) == 1
    assert projection.embedded[0].source_candidate_id == "fallback"
    assert any("not approved" in value.reason for value in projection.decisions)
    assert any("minimum dimensions" in value.reason for value in projection.decisions)


@pytest.mark.asyncio
async def test_embedded_fallback_never_becomes_a_replacement() -> None:
    repository = StubArtworkRepository()
    processor = ArtworkProcessor()
    candidate = _candidate("embedded", source="embedded")
    fallback = await processor.inspect(candidate, _png(100, 100, (10, 20, 30)))
    settings = ArtworkManagementSettings(providers=["embedded"], external_enabled=False)

    projection = await ArtworkProjectionService(repository, processor).project(
        settings=settings,
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=None,
        existing_embedded=(_existing(),),
        existing_external=(),
        embedded_fallback=(fallback,),
        priority=RequestPriority.USER_INITIATED,
    )

    assert projection.embedded == ()
    assert projection.preserved_existing is True
    assert all(value.action != "replace" for value in projection.decisions)


@pytest.mark.asyncio
async def test_processed_smaller_and_preserved_types_protect_each_file() -> None:
    repository = StubArtworkRepository()
    candidate = _candidate("large", source="cover_art_archive_release")
    repository.candidates["release"] = (candidate,)
    repository.content[candidate.candidate_id] = _png(500, 500, (1, 2, 3))
    service = ArtworkProjectionService(repository, ArtworkProcessor())

    smaller = await service.project(
        settings=ArtworkManagementSettings(
            embedded_maximum_size=100,
            external_enabled=False,
        ),
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=None,
        existing_embedded=(_existing(width=200, height=200),),
        existing_external=(),
        priority=RequestPriority.USER_INITIATED,
    )
    preserved = await service.project(
        settings=ArtworkManagementSettings(
            preserve_existing_types=["front"],
            external_enabled=False,
        ),
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=None,
        existing_embedded=(_existing(width=20, height=20),),
        existing_external=(),
        priority=RequestPriority.USER_INITIATED,
    )

    assert smaller.embedded == ()
    assert preserved.embedded == ()
    assert any("smaller" in value.reason for value in smaller.decisions)
    assert any("preserves" in value.reason for value in preserved.decisions)


@pytest.mark.asyncio
async def test_external_collision_is_preserved_unless_overwrite_is_enabled() -> None:
    repository = StubArtworkRepository()
    candidate = _candidate("front", source="cover_art_archive_release")
    repository.candidates["release"] = (candidate,)
    repository.content[candidate.candidate_id] = _png(100, 100, (4, 5, 6))
    service = ArtworkProjectionService(repository, ArtworkProcessor())

    protected = await service.project(
        settings=ArtworkManagementSettings(embedded_enabled=False),
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=None,
        existing_embedded=(),
        existing_external=(_existing(width=50, height=50),),
        priority=RequestPriority.USER_INITIATED,
    )
    replaced = await service.project(
        settings=ArtworkManagementSettings(
            embedded_enabled=False, overwrite_external_files=True
        ),
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=None,
        existing_embedded=(),
        existing_external=(_existing(width=50, height=50),),
        priority=RequestPriority.USER_INITIATED,
    )

    assert protected.external == ()
    assert len(replaced.external) == 1
    assert any("overwrite is disabled" in value.reason for value in protected.decisions)


@pytest.mark.asyncio
async def test_existing_external_artwork_is_inspected_from_real_bytes(
    tmp_path: Path,
) -> None:
    album = tmp_path / "album"
    album.mkdir()
    content = _png(321, 123, (4, 5, 6))
    (album / "cover.png").write_bytes(content)
    service = ArtworkProjectionService(StubArtworkRepository(), ArtworkProcessor())

    existing = await service.inspect_existing_external(
        ArtworkManagementSettings(), album
    )

    assert len(existing) == 1
    assert existing[0].image_type == "front"
    assert existing[0].mime_type == "image/png"
    assert existing[0].width == 321
    assert existing[0].height == 123
    assert existing[0].byte_size == len(content)


@pytest.mark.asyncio
async def test_provider_failure_preserves_existing_and_records_deferred() -> None:
    repository = StubArtworkRepository()
    repository.fail = {"release", "release-group"}

    projection = await ArtworkProjectionService(repository, ArtworkProcessor()).project(
        settings=ArtworkManagementSettings(
            providers=[
                "cover_art_archive_release",
                "cover_art_archive_release_group",
            ],
            external_enabled=False,
        ),
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=None,
        existing_embedded=(_existing(),),
        existing_external=(),
        priority=RequestPriority.BACKGROUND_SYNC,
    )

    assert projection.embedded == ()
    assert projection.preserved_existing is True
    assert projection.deferred_sources == (
        "cover_art_archive_release",
        "cover_art_archive_release_group",
    )


@pytest.mark.asyncio
async def test_local_patterns_are_case_insensitive_and_do_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    album = tmp_path / "album"
    album.mkdir()
    (album / "COVER.PNG").write_bytes(_png(64, 64, (9, 8, 7)))
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png(200, 200, (7, 8, 9)))
    (album / "folder.png").symlink_to(outside)
    settings = ArtworkManagementSettings(
        providers=["local_files"], external_enabled=False
    )

    projection = await ArtworkProjectionService(
        StubArtworkRepository(), ArtworkProcessor()
    ).project(
        settings=settings,
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=album,
        existing_embedded=(),
        existing_external=(),
        priority=RequestPriority.USER_INITIATED,
    )

    assert len(projection.embedded) == 1
    assert projection.embedded[0].source == "local_files"
    assert projection.embedded[0].width == 64


@pytest.mark.asyncio
async def test_complete_preset_collects_every_local_artwork_type(
    tmp_path: Path,
) -> None:
    album = tmp_path / "album"
    album.mkdir()
    filenames = {
        "front": "cover.png",
        "back": "back.png",
        "booklet": "booklet.png",
        "medium": "disc.png",
        "tray": "tray.png",
        "obi": "obi.png",
        "spine": "spine.png",
        "track": "track.png",
        "other": "artist-photo.png",
    }
    for index, filename in enumerate(filenames.values(), start=1):
        (album / filename).write_bytes(_png(64, 64, (index, index, index)))
    settings = complete_library_organizer_profile().artwork

    projection = await ArtworkProjectionService(
        StubArtworkRepository(), ArtworkProcessor()
    ).project(
        settings=settings,
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=album,
        existing_embedded=(),
        existing_external=(),
        priority=RequestPriority.USER_INITIATED,
    )

    assert {value.image_type for value in projection.embedded} == {"front"}
    assert {value.image_type for value in projection.external} == set(filenames)


@pytest.mark.asyncio
async def test_pdf_local_artwork_is_external_only(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    (album / "booklet.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\nendobj\n%%EOF\n")
    settings = ArtworkManagementSettings(
        providers=["local_files"],
        local_file_patterns=["booklet.pdf"],
        image_types=["booklet"],
        embedded_front_only=False,
        external_front_only=False,
        external_format="original",
    )

    projection = await ArtworkProjectionService(
        StubArtworkRepository(), ArtworkProcessor()
    ).project(
        settings=settings,
        release_mbid=_RELEASE,
        release_group_mbid=_RG,
        album_directory=album,
        existing_embedded=(),
        existing_external=(),
        priority=RequestPriority.USER_INITIATED,
    )

    assert projection.embedded == ()
    assert len(projection.external) == 1
    assert projection.external[0].mime_type == "application/pdf"
    assert projection.external[0].image_type == "booklet"


@pytest.mark.asyncio
async def test_missing_release_mbid_falls_back_instead_of_raising() -> None:
    """An album with no release MBID is ordinary missing data.

    It used to reach ``validate_mbid`` and raise ValueError, which escaped the
    planner and wedged the whole operation worker on that album forever.
    """
    repository = StubArtworkRepository()
    fallback = _candidate("group-front", source="cover_art_archive_release_group")
    repository.candidates["release-group"] = (fallback,)
    repository.content = {fallback.candidate_id: _png(300, 300, (9, 9, 9))}
    settings = ArtworkManagementSettings(external_enabled=False)

    projection = await ArtworkProjectionService(repository, ArtworkProcessor()).project(
        settings=settings,
        release_mbid="",
        release_group_mbid=_RG,
        album_directory=None,
        existing_embedded=(),
        existing_external=(),
        priority=RequestPriority.USER_INITIATED,
    )

    assert "release" not in repository.calls
    assert [value.source for value in projection.embedded] == [
        "cover_art_archive_release_group"
    ]


@pytest.mark.asyncio
async def test_no_identifiers_at_all_projects_nothing_and_does_not_raise() -> None:
    repository = StubArtworkRepository()
    settings = ArtworkManagementSettings(external_enabled=False)

    projection = await ArtworkProjectionService(repository, ArtworkProcessor()).project(
        settings=settings,
        release_mbid="",
        release_group_mbid="",
        album_directory=None,
        existing_embedded=(),
        existing_external=(),
        priority=RequestPriority.USER_INITIATED,
    )

    assert repository.calls == []
    assert projection.embedded == ()


@pytest.mark.asyncio
async def test_malformed_mbid_defers_the_provider_instead_of_raising() -> None:
    """validate_mbid also rejects non-UUID identifiers - degrade, never fail."""

    class RejectingRepository(StubArtworkRepository):
        async def list_management_artwork(self, **kwargs):
            self.calls.append(kwargs["entity_kind"])
            raise ValueError("Invalid release MBID format: unknown_42")

    repository = RejectingRepository()
    settings = ArtworkManagementSettings(external_enabled=False)

    projection = await ArtworkProjectionService(repository, ArtworkProcessor()).project(
        settings=settings,
        release_mbid="unknown_42",
        release_group_mbid="unknown_42",
        album_directory=None,
        existing_embedded=(),
        existing_external=(),
        priority=RequestPriority.USER_INITIATED,
    )

    assert projection.embedded == ()
    assert projection.deferred_sources
