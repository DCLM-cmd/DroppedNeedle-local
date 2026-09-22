import json
from pathlib import Path

import pytest

from core.config import Settings
from services.preferences_service import PreferencesService


@pytest.fixture
def prefs_service(tmp_path: Path) -> PreferencesService:
    config_path = tmp_path / "config.json"
    settings = Settings()
    settings.config_file_path = config_path
    return PreferencesService(settings)


class TestLibraryDownloadAccess:
    @pytest.mark.parametrize(
        ("access", "role", "expected"),
        [
            ("everyone", "user", True),
            ("everyone", "trusted", True),
            ("everyone", "admin", True),
            ("trusted", "user", False),
            ("trusted", "trusted", True),
            ("trusted", "admin", True),
            ("admin", "user", False),
            ("admin", "trusted", False),
            ("admin", "admin", True),
        ],
    )
    def test_is_library_download_allowed_truth_table(
        self, prefs_service: PreferencesService, access: str, role: str, expected: bool
    ) -> None:
        settings = prefs_service.get_security_settings()
        settings.library_download_access = access
        prefs_service.save_security_settings(settings)

        assert prefs_service.is_library_download_allowed(role) is expected

    def test_download_access_defaults_to_everyone(
        self, prefs_service: PreferencesService
    ) -> None:
        assert (
            prefs_service.get_security_settings().library_download_access == "everyone"
        )
        assert prefs_service.is_library_download_allowed("user") is True

    def test_old_security_blob_without_key_loads_with_default(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"security_settings": {"hibp_check": False}}),
            encoding="utf-8",
        )
        settings = Settings()
        settings.config_file_path = config_path

        fresh = PreferencesService(settings)

        assert fresh.get_security_settings().library_download_access == "everyone"
