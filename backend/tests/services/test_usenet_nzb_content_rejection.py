"""Usenet enqueue NZB content rejection (issue #266).

An indexer answering the NZB enclosure URL with an HTML error/limit page (HTTP
200, non-NZB body) is a deterministic content rejection: the release is
blocklisted by title+size and the task surfaces the safe fixed message (never
URLs, hosts, or indexer body text). Transport errors stay generic and never
blocklist.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.exceptions import NewznabApiError
from models.download import DownloadTask, ScoredCandidate
from models.download_identity import usenet_identity
from models.download_manifest import ManifestCodec
from repositories.protocols.indexer import UsenetRelease
from services.native.acquisition.errors import OrchestrationError
from services.native.acquisition.strategy import UsenetStrategy

_SAFE_MESSAGE = (
    "indexer returned a non-NZB body (likely an error/limit page), not an NZB"
)


def _release(**overrides):
    fields = dict(
        indexer_id="ds",
        indexer_name="DS",
        guid="guid-1",
        title="Artist - Album [FLAC]",
        nzb_url="https://indexer.example/getnzb/abc&i=1&r=key",
        size_bytes=350 * 1024 * 1024,
    )
    fields.update(overrides)
    return UsenetRelease(**fields)


def _candidate(release=None):
    return ScoredCandidate(
        source="usenet",
        files=[],
        usenet_release=release or _release(),
        coherence=0.9,
        file_confidence=0.9,
        final_score=0.9,
        tier="auto",
    )


def _task(**overrides) -> DownloadTask:
    kwargs = dict(
        id="t1",
        user_id="u1",
        download_type="track",
        release_group_mbid="rg-1",
        release_mbid="release-1",
        release_track_mbid="release-track-1",
        artist_name="Artist",
        album_title="Album",
        year=2020,
        track_count=1,
        track_title="Track 1",
        recording_mbid="recording-1",
        track_number=1,
        disc_number=1,
        track_duration_seconds=200.0,
        origin="user",
        candidate_index=0,
    )
    kwargs.update(overrides)
    return DownloadTask(**kwargs)


def _content_rejection():
    error = NewznabApiError(
        _SAFE_MESSAGE,
        details={
            "status": 200,
            "content_type": "text/html",
            "snippet": "<html>Limit reached</html>",
        },
        code=200,
    )
    error.content_rejection = True
    return error


def _strategy(tmp_path: Path, *, client):
    store = AsyncMock()
    store.create_download_attempt.return_value = SimpleNamespace(id="attempt-1")
    strategy = UsenetStrategy(
        indexer=MagicMock(),
        scorer=MagicMock(),
        client=client,
        store=store,
        file_processor=MagicMock(),
        import_settle_seconds=0,
        staging=tmp_path,
        manifest_codec=ManifestCodec(),
        naming_template="{albumartist}/{album}/{title}.{ext}",
        album_service=MagicMock(),
        category=None,
        priority=None,
        post_processing=None,
        min_release_age_seconds=0,
    )
    return strategy, store


@pytest.mark.asyncio
async def test_content_rejection_surfaces_safe_message_and_blocklists(
    tmp_path: Path,
):
    client = AsyncMock()
    client.enqueue = AsyncMock(side_effect=_content_rejection())
    strategy, store = _strategy(tmp_path, client=client)

    with pytest.raises(OrchestrationError) as exc_info:
        await strategy.enqueue(_task(), _candidate(), strict_track_duration=True)

    message = str(exc_info.value)
    assert _SAFE_MESSAGE in message
    assert "<html>" not in message  # the indexer snippet must never surface
    assert "indexer.example" not in message  # nor the enclosure URL/host
    store.record_quarantine.assert_awaited_once_with(
        source="usenet",
        identity=usenet_identity("Artist - Album [FLAC]", 350 * 1024 * 1024),
        reason="download_failed",
        release_group_mbid="rg-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_error",
    [
        NewznabApiError("NZB fetch failed: connection reset"),
        NewznabApiError("NZB fetch returned HTTP 403", details="forbidden", code=403),
    ],
)
async def test_non_content_errors_stay_generic_and_never_blocklist(
    tmp_path: Path, client_error
):
    # Transport failures and HTTP errors carry no content-rejection marker: the
    # task keeps the generic message and the release is NOT quarantined (it may
    # succeed on retry).
    client = AsyncMock()
    client.enqueue = AsyncMock(side_effect=client_error)
    strategy, store = _strategy(tmp_path, client=client)

    with pytest.raises(OrchestrationError) as exc_info:
        await strategy.enqueue(_task(), _candidate(), strict_track_duration=True)

    assert str(exc_info.value) == "enqueue failed"
    store.record_quarantine.assert_not_awaited()
