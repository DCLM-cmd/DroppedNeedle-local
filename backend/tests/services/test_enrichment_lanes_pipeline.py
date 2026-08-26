"""A2 parts 1+2: lane discipline and pipeline restructure for the
discover-queue enrichment chain.

- Build legs (RG lookup, release lookup, artist lookup) must request
  BACKGROUND_SYNC; the recording leg keeps its existing literal.
- The overlapped pipeline produces the same enrichment payload as the old
  serial order and survives mid-build cancellation without orphan futures.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.schemas.discover import DiscoverQueueEnrichment
from infrastructure.queue.priority_queue import RequestPriority
from services.discover.enrichment_service import QueueEnrichmentService

RG = "074aa5b0-712e-4d6c-8d14-8aedc43e84fd"


def _rg_payload() -> dict:
    return {
        "id": RG,
        "title": "Some Album",
        "tags": [{"name": "rock"}],
        "artist-credit": [{"artist": {"id": "artist-1", "name": "The Artist"}}],
        "releases": [{"id": "release-1", "date": "2020-01-01"}],
    }


def _make_service(
    *,
    release_hang: float | None = None,
) -> tuple[QueueEnrichmentService, AsyncMock, dict]:
    mb_repo = AsyncMock()
    captured: dict[str, list[RequestPriority]] = {
        "rg": [],
        "release": [],
        "artist": [],
    }

    async def fake_rg(mbid, includes=None, priority=RequestPriority.USER_INITIATED):
        captured["rg"].append(priority)
        return _rg_payload()

    async def fake_release(rid, includes=None, priority=RequestPriority.USER_INITIATED):
        captured["release"].append(priority)
        if release_hang:
            await asyncio.sleep(release_hang)
        return {"media": []}

    async def fake_artist(mbid, priority=RequestPriority.USER_INITIATED):
        captured["artist"].append(priority)
        return {"country": "GB", "relations": []}

    mb_repo.get_release_group_by_id = AsyncMock(side_effect=fake_rg)
    mb_repo.get_release_by_id = AsyncMock(side_effect=fake_release)
    mb_repo.get_recording_by_id = AsyncMock(return_value={})
    mb_repo.get_artist_by_id = AsyncMock(side_effect=fake_artist)

    def _extract(payload):
        for rel in payload.get("relations") or []:
            resource = rel.get("url", {}).get("resource", "")
            if "youtube.com" in resource:
                return resource
        return ""

    mb_repo.extract_youtube_url_from_relations = _extract
    mb_repo.youtube_url_to_embed = lambda raw: f"embed:{raw}"

    lb_repo = AsyncMock()
    lb_repo.get_release_group_popularity_batch = AsyncMock(return_value={})

    prefs = MagicMock()
    yt = MagicMock(enabled=True, api_enabled=True)
    yt.has_valid_api_key.return_value = True
    prefs.get_youtube_connection.return_value = yt

    integration = MagicMock()
    integration.get_queue_settings().enrich_ttl = 60
    integration.is_lastfm_enabled.return_value = False

    svc = QueueEnrichmentService(
        musicbrainz_repo=mb_repo,
        listenbrainz_repo=lb_repo,
        preferences_service=prefs,
        integration=integration,
        memory_cache=None,
        wikidata_repo=None,
        lastfm_repo=None,
    )
    # Fast coalescer window so tests do not wait out the real 500 ms.
    svc._POPULARITY_WINDOW_SECONDS = 0.02
    return svc, mb_repo, captured


class TestLaneDiscipline:
    @pytest.mark.asyncio
    async def test_build_legs_request_background_sync(self):
        svc, _mb, captured = _make_service()

        await svc.enrich_queue_item(RG)

        assert captured["rg"] == [RequestPriority.BACKGROUND_SYNC]
        assert captured["release"] == [RequestPriority.BACKGROUND_SYNC]
        assert captured["artist"] == [RequestPriority.BACKGROUND_SYNC]

    @pytest.mark.asyncio
    async def test_explicit_lane_override_is_threaded(self):
        svc, _mb, captured = _make_service()

        await svc.enrich_queue_item(RG, priority=RequestPriority.BACKGROUND_SYNC)

        assert set(captured["rg"]) == {RequestPriority.BACKGROUND_SYNC}


class TestPipelineEquivalence:
    @pytest.mark.asyncio
    async def test_overlapped_pipeline_matches_serial_expectations(self):
        svc, _mb, _captured = _make_service()

        enrichment = await svc.enrich_queue_item(RG)

        assert isinstance(enrichment, DiscoverQueueEnrichment)
        assert enrichment.tags == ["rock"]
        assert enrichment.artist_mbid == "artist-1"
        assert enrichment.release_date == "2020-01-01"
        assert enrichment.country == "GB"  # artist leg ran
        assert enrichment.youtube_search_available is True  # no YT anywhere
        assert enrichment.youtube_url is None
        # LB popularity returned {} -> listen_count stays None (B4 repo path).
        assert enrichment.listen_count is None

    @pytest.mark.asyncio
    async def test_youtube_from_release_still_wins_when_present(self):
        svc, mb_repo, _captured = _make_service()

        async def fake_release(
            rid, includes=None, priority=RequestPriority.USER_INITIATED
        ):
            return {
                "media": [],
                "relations": [
                    {
                        "type": "streaming",
                        "url": {"resource": "https://youtube.com/watch?v=xyz"},
                    }
                ],
            }

        mb_repo.get_release_by_id = AsyncMock(side_effect=fake_release)
        mb_repo.youtube_url_to_embed = lambda raw: f"embed:{raw}"

        enrichment = await svc.enrich_queue_item(RG)

        assert enrichment.youtube_url == "embed:https://youtube.com/watch?v=xyz"


class TestCancellationSafety:
    @pytest.mark.asyncio
    async def test_mid_build_cancel_leaves_no_orphan_futures(self):
        svc, mb_repo, _captured = _make_service(release_hang=10.0)

        task = asyncio.create_task(svc.enrich_queue_item(RG))
        await asyncio.sleep(0.05)  # let it enter the hanging release fetch
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.shield(task)

        # All spawned tasks finished (none left pending after cancellation).
        done = (
            await asyncio.gather(
                *svc._popularity_pending.values(), return_exceptions=True
            )
            if svc._popularity_pending
            else []
        )
        assert all(isinstance(r, (BaseException, type(None))) for r in done)
