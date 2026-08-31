"""An unstamped build must still carry a version, and a failure must name itself.

Both defects showed up together on the live server. The Dockerfile writes
``ENV COMMIT_TAG=${COMMIT_TAG}`` from an ARG nothing passes, so the variable exists
and is EMPTY - and ``os.environ.get(key, default)`` only falls back when the key is
ABSENT. The User-Agent became "DroppedNeedleApp/ (...)" with no version, which
MetaBrainz throttles; the ListenBrainz and Cover Art Archive circuit breakers both
sat open.

Diagnosing that took far longer than it should have, because the retry logger
formatted the exception with "%s": every httpx timeout has an EMPTY str(), so the
log read "failed after 3 attempts: " and named neither the error nor its type.
"""

import logging

import httpx
import pytest

from core.config import build_version


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_an_empty_commit_tag_still_yields_a_version(monkeypatch, value) -> None:
    """The regression: present-but-empty is what a plain default does not catch."""
    monkeypatch.setenv("COMMIT_TAG", value)
    assert build_version() == "dev"


def test_a_missing_commit_tag_yields_a_version(monkeypatch) -> None:
    monkeypatch.delenv("COMMIT_TAG", raising=False)
    assert build_version() == "dev"


def test_a_real_commit_tag_is_used_verbatim(monkeypatch) -> None:
    monkeypatch.setenv("COMMIT_TAG", "v2.4.1")
    assert build_version() == "v2.4.1"


def test_the_user_agent_always_names_a_version(monkeypatch) -> None:
    """MetaBrainz requires application AND version; without one they throttle."""
    from core.config import Settings

    monkeypatch.setenv("COMMIT_TAG", "")
    agent = Settings(instance_id="0123456789abcdef").get_user_agent()

    assert agent.startswith("DroppedNeedleApp/dev ")
    assert "DroppedNeedleApp/ " not in agent
    assert "contact@" in agent


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout(""),
        httpx.ReadTimeout(""),
        httpx.PoolTimeout(""),
        ValueError(),
        RuntimeError(""),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_line_names_the_exception_type(caplog, exc) -> None:
    """Every one of these has an EMPTY str(), which is what made the log unreadable.

    The retry decorator now logs the type alongside the message at its single
    failure site, so the line identifies the failure even when the exception
    carries no text of its own.
    """
    from infrastructure.resilience.retry import with_retry

    @with_retry(max_attempts=1, base_delay=0, retriable_exceptions=(Exception,))
    async def always_fails() -> None:
        raise exc

    with caplog.at_level(logging.ERROR, logger="infrastructure.resilience.retry"):
        with pytest.raises(type(exc)):
            await always_fails()

    line = " ".join(record.getMessage() for record in caplog.records)
    assert type(exc).__name__ in line, line
