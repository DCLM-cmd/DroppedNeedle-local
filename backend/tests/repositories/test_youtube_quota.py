import asyncio
import inspect
import threading
from unittest.mock import AsyncMock

import httpx
import msgspec
import pytest

from api.v1.schemas.settings import YouTubeConnectionSettings
from core.exceptions import ConfigurationError, ExternalServiceError, InvalidExternalPayloadError, RateLimitedError
from infrastructure.observability.optional_work import OptionalWorkDeferred, optional_work_budget
from infrastructure.persistence import youtube_quota_store
from repositories import youtube
from repositories.protocols.youtube import YouTubeRepositoryProtocol


def response(payload=None, status=200):
    return httpx.Response(status, json=payload if payload is not None else {"items": [{"id": {"videoId": "abcdefghijk"}}]})


@pytest.fixture
def setup_repo(tmp_path, monkeypatch):
    path = tmp_path / "youtube_quota.json"
    monkeypatch.setattr(youtube, "get_quota_file_path", lambda: path)
    settings = YouTubeConnectionSettings(api_key="secret", enabled=True, api_enabled=True, daily_quota_limit=1)
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = response()
    repo = youtube.YouTubeRepository(client, settings_getter=lambda: settings)
    return repo, client, settings, path


@pytest.mark.asyncio
async def test_two_users_and_repositories_cannot_spend_last_slot_twice(setup_repo):
    repo, client, settings, path = setup_repo
    other = youtube.YouTubeRepository(client, settings_getter=lambda: settings)
    results = await asyncio.gather(repo.search_video("a", "one"), other.search_track("a", "two"), return_exceptions=True)
    assert sum(value == "abcdefghijk" for value in results) == 1
    assert sum(isinstance(value, RateLimitedError) for value in results) == 1
    assert client.get.await_count == 1
    assert msgspec.json.decode(path.read_bytes())["count"] == 1


@pytest.mark.asyncio
async def test_identical_search_joins_and_cancelled_waiter_does_not_cancel_survivor(setup_repo):
    repo, client, _, _ = setup_repo
    entered, finish = asyncio.Event(), asyncio.Event()

    async def get(*args, **kwargs):
        entered.set()
        await finish.wait()
        return response()

    client.get.side_effect = get
    first = asyncio.create_task(repo.search_video("Artist", "Album"))
    await entered.wait()
    survivor = asyncio.create_task(repo.search_video("artist", "album"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    assert await survivor == "abcdefghijk"
    assert client.get.await_count == 1
    assert repo.get_quota_status().used == 1


@pytest.mark.asyncio
async def test_album_track_identity_and_cached_urls_when_disabled(setup_repo):
    repo, client, settings, _ = setup_repo
    assert await repo.search_video("artist", "same") == "abcdefghijk"
    settings.enabled = False
    assert await repo.search_video("artist", "same") == "abcdefghijk"
    with pytest.raises(ConfigurationError):
        await repo.search_track("artist", "same")
    settings.enabled = True
    settings.api_enabled = False
    with pytest.raises(ConfigurationError):
        await repo.search_track("artist", "same")
    settings.api_enabled = True
    settings.daily_quota_limit = 2
    client.get.return_value = response({"items": [{"id": {"videoId": "other-video"}}]})
    assert await repo.search_track("artist", "same") == "other-video"
    assert await repo.search_video("artist", "same") == "abcdefghijk"
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_recreation_and_utc_rollover_keep_durable_count(setup_repo, monkeypatch):
    repo, client, settings, path = setup_repo
    today = ["2026-09-06"]
    monkeypatch.setattr(youtube, "utc_today", lambda: today[0])
    monkeypatch.setattr(youtube_quota_store, "utc_today", lambda: today[0])
    await repo.search_video("a", "old")
    youtube._states.pop(path.resolve())
    recreated = youtube.YouTubeRepository(client, settings_getter=lambda: settings)
    with pytest.raises(RateLimitedError):
        await recreated.search_video("a", "blocked")
    today[0] = "2026-09-07"
    assert await recreated.search_video("a", "new") == "abcdefghijk"
    assert msgspec.json.decode(path.read_bytes()) == {"date": today[0], "count": 1}
    assert client.get.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["403", "transport", "decode"])
async def test_fresh_failures_are_typed_and_charged_not_cached(setup_repo, failure):
    repo, client, settings, path = setup_repo
    if failure == "transport":
        client.get.side_effect = httpx.ReadTimeout("ambiguous transmission")
    elif failure == "decode":
        client.get.return_value = httpx.Response(200, content=b"not json")
    else:
        client.get.return_value = response(status=403)
    error = InvalidExternalPayloadError if failure == "decode" else ExternalServiceError
    with pytest.raises(error):
        await repo.search_video("a", "album")
    assert msgspec.json.decode(path.read_bytes())["count"] == 1
    assert not repo.is_cached("a", "album")
    settings.daily_quota_limit = 2
    client.get.side_effect = None
    client.get.return_value = response()
    assert await repo.search_video("a", "album") == "abcdefghijk"
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_successful_empty_result_is_cached_absence(setup_repo):
    repo, client, _, _ = setup_repo
    client.get.return_value = response({"items": []})
    assert await repo.search_track("a", "t") is None
    assert await repo.search_track("a", "t") is None
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_persistence_failure_prevents_http_and_refunds_optional_budget(setup_repo, monkeypatch):
    repo, client, _, _ = setup_repo

    def fail(state):
        raise OSError("disk full")

    monkeypatch.setattr(repo._state.quota, "_write", fail)
    with optional_work_budget() as budget:
        with pytest.raises(ExternalServiceError):
            await repo.search_video("a", "album")
        assert budget.remaining == 10
    client.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_reservation_settles_before_dispatch_and_disabled_or_cancelled_work_refunds(setup_repo, monkeypatch, cancel):
    repo, client, settings, path = setup_repo
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = repo._state.quota._write

    def paused_write(state):
        if state.count == 1:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
        original(state)

    monkeypatch.setattr(repo._state.quota, "_write", paused_write)
    task = asyncio.create_task(repo.search_video("a", "album"))
    try:
        await entered.wait()
        client.get.assert_not_awaited()
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
        else:
            settings.api_enabled = False
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError if cancel else ConfigurationError):
        await task
    client.get.assert_not_awaited()
    assert msgspec.json.decode(path.read_bytes())["count"] == 0


@pytest.mark.asyncio
async def test_on_wire_cancellation_retains_reservation(setup_repo):
    repo, client, _, path = setup_repo
    entered = asyncio.Event()

    async def get(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    client.get.side_effect = get
    task = asyncio.create_task(repo.search_video("a", "album"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert msgspec.json.decode(path.read_bytes())["count"] == 1


@pytest.mark.asyncio
async def test_optional_budget_only_charges_uncached_physical_owner(setup_repo):
    repo, client, _, _ = setup_repo
    with optional_work_budget() as budget:
        budget.remaining = 1
        assert await asyncio.gather(repo.search_video("a", "album"), repo.search_video("a", "album")) == ["abcdefghijk", "abcdefghijk"]
        assert await repo.search_video("a", "album") == "abcdefghijk"
        with pytest.raises(OptionalWorkDeferred):
            await repo.search_track("a", "track")
        assert budget.remaining == 0
    assert client.get.await_count == 1


def test_youtube_protocol_signatures():
    for name, method in vars(YouTubeRepositoryProtocol).items():
        if name.startswith("_"):
            continue
        implementation = getattr(youtube.YouTubeRepository, name)
        if isinstance(method, property):
            method, implementation = method.fget, implementation.fget
        assert inspect.signature(implementation) == inspect.signature(method)


@pytest.mark.asyncio
async def test_foreground_join_survives_optional_guard_disabling(setup_repo, monkeypatch):
    repo, client, _, path = setup_repo
    entered, release = asyncio.Event(), asyncio.Event()
    original = repo._state.quota.reserve

    async def paused_reserve(limit):
        date = await original(limit)
        entered.set()
        await release.wait()
        return date

    monkeypatch.setattr(repo._state.quota, "reserve", paused_reserve)
    with optional_work_budget() as budget:
        background = asyncio.create_task(repo.search_video("a", "album"))
        await entered.wait()
    foreground = asyncio.create_task(repo.search_video("a", "album"))
    await asyncio.sleep(0)
    budget.guard = lambda: False
    release.set()
    with pytest.raises(OptionalWorkDeferred):
        await background
    assert await foreground == "abcdefghijk"
    assert client.get.await_count == 1
    assert msgspec.json.decode(path.read_bytes())["count"] == 1
    assert budget.remaining == 10
