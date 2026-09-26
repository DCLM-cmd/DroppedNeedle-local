"""Outbound pacing towards MetaBrainz.

MetaBrainz blocked this application by NAME - every request carrying it is refused
regardless of address, version or token. The likely earning of that: a warmer that
issues three calls per artist across the whole library, paced at the documented
ceiling rather than well under it, and that kept going once the answer was "stop".
That artist-discovery warmer has since been replaced upstream by demand scheduling.
"""


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

