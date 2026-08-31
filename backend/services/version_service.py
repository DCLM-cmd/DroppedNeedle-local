import logging
import os

from packaging.version import InvalidVersion, Version

from api.v1.schemas.version import GitHubRelease, UpdateCheckResponse, VersionInfo
from core.exceptions import ConfigurationError
from core.config import build_version
from repositories.github_repository import GitHubRepository

logger = logging.getLogger(__name__)


class VersionService:
    def __init__(self, github_repo: GitHubRepository, preferences=None) -> None:  # noqa: ANN001
        self._github_repo = github_repo
        # Optional so every existing construction (and the tests) keeps working; when
        # absent the server behaves as it always did and announces updates.
        self._preferences = preferences

    def _notifications_enabled(self) -> bool:
        if self._preferences is None:
            return True
        try:
            return bool(self._preferences.get_preferences().notify_new_versions)
        except (OSError, ValueError, ConfigurationError):
            # A preferences read that FAILS must not break /version. A missing method
            # or attribute is a wiring bug, not a read failure, and is deliberately
            # left to raise - swallowing it would make the setting silently do
            # nothing, which is worse than an error nobody can act on.
            logger.warning("Could not read the update-notification preference")
            return True

    def get_current_version(self) -> VersionInfo:
        # build_version(), not os.environ.get(..., "dev"): an unstamped build leaves
        # COMMIT_TAG present-but-empty, which the plain default does not catch.
        build_date = (os.environ.get("BUILD_DATE") or "").strip() or None
        return VersionInfo(version=build_version(), build_date=build_date)

    async def check_for_updates(self) -> UpdateCheckResponse:
        current = self.get_current_version()
        latest = await self._github_repo.fetch_latest_release()

        if latest is None:
            return UpdateCheckResponse(current_version=current.version)

        update_available, comparison_failed = self._is_newer(
            latest.tag_name, current.version
        )

        # Dev builds: simulate update available so the full UI can be tested
        is_dev = current.version in ("dev", "hosting-local")
        if comparison_failed and is_dev:
            update_available = True

        # The version numbers stay truthful either way - only the ANNOUNCEMENT is
        # suppressed, because "stop interrupting me about updates" is not a request to
        # be told the wrong version on the About page.
        if not self._notifications_enabled():
            update_available = False

        return UpdateCheckResponse(
            current_version=current.version,
            latest_version=latest.tag_name,
            update_available=update_available,
            comparison_failed=comparison_failed,
            latest_release=latest if update_available else None,
        )

    async def get_release_history(self) -> list[GitHubRelease]:
        return await self._github_repo.fetch_releases()

    @staticmethod
    def _is_newer(latest_tag: str, current_tag: str) -> tuple[bool, bool]:
        """Compare version tags. Returns (update_available, comparison_failed)."""
        try:
            latest = Version(latest_tag.lstrip("v"))
            current = Version(current_tag.lstrip("v"))
            return (latest > current, False)
        except InvalidVersion:
            logger.warning(
                "Invalid version comparison: %s vs %s", latest_tag, current_tag
            )
            return (False, True)
