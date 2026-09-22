"""_expected_tracks_for_task: the enqueue manifest measures the pinned edition's
AUDIO positions only, so request targets and the coverage gate agree."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.album import Track
from services.native.acquisition.errors import OrchestrationError
from services.native.acquisition.strategy import _expected_tracks_for_task


def _track(position, disc, title, medium):
    return Track(
        position=position,
        title=title,
        disc_number=disc,
        length=180000,
        recording_id=f"rec-{disc}-{position}",
        release_track_id=f"rt-{disc}-{position}",
        media_format=medium,
    )


def _album_task(**overrides):
    values = {
        "download_type": "album",
        "track_count": 4,
        "release_mbid": "rel-1",
        "release_group_mbid": "rg-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _album_service(exact_tracks=None, group_tracks=None):
    svc = MagicMock()
    svc.get_exact_edition_tracks_info = AsyncMock(
        return_value=SimpleNamespace(tracks=list(exact_tracks or []))
    )
    svc.get_album_tracks_info = AsyncMock(
        return_value=SimpleNamespace(
            tracks=list(group_tracks or []), selected_release_mbid="rel-1"
        )
    )
    return svc


def _cd_dvd_edition():
    return [
        _track(1, 1, "Audio One", "CD"),
        _track(2, 1, "Audio Two", "CD"),
        _track(1, 2, "Video One", "DVD"),
        _track(2, 2, "Video Two", "DVD"),
    ]


@pytest.mark.asyncio
async def test_manifest_excludes_video_positions_from_pinned_edition():
    svc = _album_service(exact_tracks=_cd_dvd_edition())

    release_mbid, expected = await _expected_tracks_for_task(
        _album_task(), svc, task_store=None
    )

    assert release_mbid == "rel-1"
    assert [track.title for track in expected] == ["Audio One", "Audio Two"]
    assert [(track.disc_number, track.track_number) for track in expected] == [
        (1, 1),
        (1, 2),
    ]


@pytest.mark.asyncio
async def test_manifest_legacy_task_filters_group_resolver_output():
    svc = _album_service(group_tracks=_cd_dvd_edition())

    release_mbid, expected = await _expected_tracks_for_task(
        _album_task(release_mbid=None), svc, task_store=None
    )

    assert release_mbid == "rel-1"
    assert [track.title for track in expected] == ["Audio One", "Audio Two"]


@pytest.mark.asyncio
async def test_manifest_strict_validation_still_applies_to_audio_set():
    bad = _cd_dvd_edition()
    bad[0] = Track(
        position=1,
        title="Audio One",
        disc_number=1,
        length=180000,
        recording_id=None,  # MB gave no recording: unmappable, fail closed
        release_track_id="rt-1-1",
        media_format="CD",
    )
    svc = _album_service(exact_tracks=bad)

    with pytest.raises(OrchestrationError, match="incomplete track map"):
        await _expected_tracks_for_task(_album_task(), svc, task_store=None)


@pytest.mark.asyncio
async def test_manifest_all_video_edition_fails_closed_not_empty():
    svc = _album_service(
        exact_tracks=[
            _track(1, 1, "Video One", "DVD"),
            _track(2, 1, "Video Two", "DVD-Video"),
        ]
    )

    with pytest.raises(OrchestrationError, match="incomplete track map"):
        await _expected_tracks_for_task(_album_task(), svc, task_store=None)


@pytest.mark.asyncio
async def test_manifest_track_path_uses_task_identity_untouched():
    task = SimpleNamespace(
        download_type="track",
        track_count=1,
        release_mbid="rel-1",
        release_track_mbid="rt-9",
        recording_mbid="rec-9",
        track_number=9,
        disc_number=2,
        track_duration_seconds=200.0,
        track_title="Video Single",
        release_group_mbid="rg-1",
    )

    release_mbid, expected = await _expected_tracks_for_task(
        task, MagicMock(), task_store=None
    )

    assert release_mbid == "rel-1"
    assert len(expected) == 1
    assert expected[0].title == "Video Single"
    assert expected[0].disc_number == 2
