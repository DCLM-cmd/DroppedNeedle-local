"""Outbound pacing towards MetaBrainz, and not walking on when told to stop.

MetaBrainz blocked this application by NAME - every request carrying it is refused
regardless of address, version or token. The likely earning of that: a warmer that
issues three calls per artist across the whole library, paced at the documented
ceiling rather than well under it, and that kept going once the answer was "stop".
"""

import pytest

from core.exceptions import RateLimitedError


def test_listenbrainz_is_paced_well_under_the_documented_ceiling():
    from repositories import listenbrainz_repository as lb

    assert lb._listenbrainz_rate_limiter.rate <= 1.0, (
        "2.5/s sustained is ~9000 requests an hour from one address"
    )


def test_cover_art_archive_is_paced_too():
    """It is MetaBrainz infrastructure and shares their expectations."""
    from repositories import coverart_repository as caa

    assert caa._coverart_rate_limiter.rate <= 4.0


def test_the_user_agent_carries_the_configured_name_and_contact():
    from core.config import Settings

    agent = Settings(
        instance_id="0123456789ab",
        app_name="NilsMusicRequestService",
        contact_email="someone@example.com",
        app_url="http://example.invalid",
    ).get_user_agent()

    assert agent.startswith("NilsMusicRequestService/")
    assert "someone@example.com" in agent
    # a version is always present - MetaBrainz throttle a name without one
    assert "NilsMusicRequestService/ (" not in agent


@pytest.mark.asyncio
async def test_the_warmer_abandons_its_pass_when_the_provider_says_stop(monkeypatch):
    """Walking the remaining artists asks a refusing provider once per artist -
    hundreds of refusals a minute on a large library."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import core.tasks as tasks

    calls = {"n": 0}

    async def precache(mbids, delay=0.0):  # noqa: ANN001, ARG001
        calls["n"] += 1
        raise RateLimitedError("stop", retry_after_seconds=60)

    service = SimpleNamespace(precache_artist_discovery=precache)
    library_db = AsyncMock()
    library_db.get_artist_mbid_page.return_value = [
        f"00000000-0000-4000-8000-{i:012d}" for i in range(25)
    ]

    real_sleep = asyncio.sleep

    # Skip the startup wait, then end the task at the between-passes sleep so exactly
    # ONE pass is measured - the outer loop is meant to come back in four hours.
    async def controlled(seconds, *args, **kwargs):  # noqa: ANN001, ARG001
        if seconds == 3600:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(tasks.asyncio, "sleep", controlled)

    try:
        await tasks.warm_artist_discovery_cache_periodically(
            lambda: service, library_db, interval=3600, delay=0
        )
    except asyncio.CancelledError:
        pass

    assert calls["n"] >= 1, "the warmer never ran"
    assert calls["n"] < 25, (
        f"kept going through the page after being refused ({calls['n']} calls)"
    )
