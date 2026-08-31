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

import httpx
import pytest

from core.config import build_version
from infrastructure.resilience.retry import _describe_exception


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
def test_an_exception_with_no_message_still_names_its_type(exc) -> None:
    """The regression: these all format as "" under "%s"."""
    described = _describe_exception(exc)

    assert described == type(exc).__name__
    assert described.strip()


def test_an_exception_with_a_message_keeps_it() -> None:
    assert _describe_exception(ValueError("bad mbid")) == "ValueError: bad mbid"


def test_the_description_is_never_empty() -> None:
    """The property the log line depends on."""
    for exc in (Exception(), Exception(""), Exception("   "), Exception("x")):
        assert _describe_exception(exc).strip()
