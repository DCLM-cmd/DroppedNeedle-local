"""Turning off update notifications silences the announcement, not the truth.

"Stop telling me about new versions" is a request about interruptions. The About
page must still report the real current and latest version - a setting that made the
server lie about which version it runs would be worse than the banner.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.v1.schemas.settings import UserPreferences
from api.v1.schemas.version import GitHubRelease
from services.version_service import VersionService


def _release(tag="v9.9.9"):
    return GitHubRelease(tag_name=tag, published_at="2026-01-01", html_url="http://x")


def _service(*, notify, current="v1.0.0", monkeypatch=None):
    repo = MagicMock()
    repo.fetch_latest_release = AsyncMock(return_value=_release())
    prefs = MagicMock()
    prefs.get_preferences = MagicMock(
        return_value=UserPreferences(notify_new_versions=notify)
    )
    service = VersionService(repo, preferences=prefs)
    service.get_current_version = lambda: SimpleNamespace(
        version=current, build_date=None
    )
    return service


@pytest.mark.asyncio
async def test_an_update_is_announced_by_default() -> None:
    result = await _service(notify=True).check_for_updates()

    assert result.update_available is True
    assert result.latest_version == "v9.9.9"


@pytest.mark.asyncio
async def test_turning_notifications_off_silences_the_announcement() -> None:
    result = await _service(notify=False).check_for_updates()

    assert result.update_available is False


@pytest.mark.asyncio
async def test_the_versions_stay_truthful_when_silenced() -> None:
    """The regression this guards: silencing must not become misreporting."""
    result = await _service(notify=False, current="v1.0.0").check_for_updates()

    assert result.current_version == "v1.0.0"
    assert result.latest_version == "v9.9.9"


@pytest.mark.asyncio
async def test_a_server_without_preferences_still_announces() -> None:
    """Every existing construction passes no preferences; behaviour must not change."""
    repo = MagicMock()
    repo.fetch_latest_release = AsyncMock(return_value=_release())
    service = VersionService(repo)
    service.get_current_version = lambda: SimpleNamespace(
        version="v1.0.0", build_date=None
    )

    assert (await service.check_for_updates()).update_available is True


@pytest.mark.asyncio
async def test_an_unreadable_preference_does_not_break_the_version_check() -> None:
    repo = MagicMock()
    repo.fetch_latest_release = AsyncMock(return_value=_release())
    prefs = MagicMock()
    prefs.get_preferences = MagicMock(side_effect=OSError("config unreadable"))
    service = VersionService(repo, preferences=prefs)
    service.get_current_version = lambda: SimpleNamespace(
        version="v1.0.0", build_date=None
    )

    assert (await service.check_for_updates()).update_available is True


def test_the_preference_defaults_to_notifying() -> None:
    assert UserPreferences().notify_new_versions is True
