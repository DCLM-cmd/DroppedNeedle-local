"""PreferencesService Prowlarr connection: masked/preserve/encrypt single section,
raw-key readiness OR-arm, save/load round-trip."""

import json
from pathlib import Path

import pytest

from api.v1.schemas.settings import (
    PROWLARR_API_KEY_MASK,
    ProwlarrConnectionSettings,
    SabnzbdConnectionSettings,
)
from core.config import Settings
from services.preferences_service import PreferencesService


@pytest.fixture
def prefs(tmp_path: Path) -> PreferencesService:
    settings = Settings()
    settings.config_file_path = tmp_path / "config.json"
    return PreferencesService(settings)


def _enable_sabnzbd(prefs: PreferencesService) -> None:
    prefs.save_sabnzbd_connection(
        SabnzbdConnectionSettings(enabled=True, url="http://sab:8080", api_key="k")
    )


def test_disabled_by_default(prefs):
    assert prefs.get_prowlarr_connection() == ProwlarrConnectionSettings()
    assert prefs.get_prowlarr_connection_raw() == ProwlarrConnectionSettings()
    assert prefs.is_prowlarr_configured() is False


def test_save_encrypts_key_and_round_trips(prefs):
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(
            enabled=True, url="http://prowlarr:9696", api_key="secret"
        )
    )
    stored = json.loads(prefs._config_path.read_text())["prowlarr"]
    assert stored["api_key"] not in ("", "secret")  # ciphertext at rest
    assert stored["enabled"] is True
    assert stored["url"] == "http://prowlarr:9696"
    raw = prefs.get_prowlarr_connection_raw()
    assert (raw.enabled, raw.url, raw.api_key) == (
        True,
        "http://prowlarr:9696",
        "secret",
    )


def test_key_masked_on_read(prefs):
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(
            enabled=True, url="http://prowlarr:9696", api_key="secret"
        )
    )
    assert prefs.get_prowlarr_connection().api_key == PROWLARR_API_KEY_MASK


def test_masked_save_preserves_key(prefs):
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(
            enabled=True, url="http://prowlarr:9696", api_key="secret"
        )
    )
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(
            enabled=False, url="http://prowlarr:9696", api_key=PROWLARR_API_KEY_MASK
        )
    )
    raw = prefs.get_prowlarr_connection_raw()
    assert raw.api_key == "secret"  # preserved
    assert raw.enabled is False  # updated


def test_whitespace_stripped_from_key_and_url(prefs):
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(
            enabled=True, url="  prowlarr:9696  ", api_key=" \tsecret\n"
        )
    )
    raw = prefs.get_prowlarr_connection_raw()
    assert raw.api_key == "secret"
    assert raw.url == "http://prowlarr:9696"


def test_url_suffix_stripped(prefs):
    settings = ProwlarrConnectionSettings(url="https://prowlarr:9696/api/v1")
    assert settings.url == "https://prowlarr:9696"
    assert ProwlarrConnectionSettings(url="").url == ""


def test_search_backend_defaults_to_indexers(prefs):
    assert prefs.get_usenet_search_backend() == "indexers"


def test_search_backend_round_trip_and_unknown_collapses(prefs):
    prefs.save_usenet_search_backend("prowlarr")
    assert prefs.get_usenet_search_backend() == "prowlarr"
    prefs.save_usenet_search_backend("indexers")
    assert prefs.get_usenet_search_backend() == "indexers"
    with pytest.raises(Exception, match="Unknown Usenet search backend"):
        prefs.save_usenet_search_backend("lidarr")


def test_readiness_either_or_prowlarr_selected(prefs):
    # SABnzbd + Prowlarr, zero Newznab rows, backend=prowlarr: Usenet is ready.
    _enable_sabnzbd(prefs)
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(
            enabled=True, url="http://prowlarr:9696", api_key="secret"
        )
    )
    assert prefs.is_usenet_ready() is False  # backend still "indexers": no rows
    prefs.save_usenet_search_backend("prowlarr")
    assert prefs.is_usenet_ready() is True
    assert prefs.is_download_source_ready() is True


def test_readiness_unselected_side_does_not_count(prefs):
    # Prowlarr configured but backend=indexers with zero rows: not ready.
    # (And symmetrically: native rows don't count when backend=prowlarr.)
    from api.v1.schemas.settings import NewznabIndexerSettings

    _enable_sabnzbd(prefs)
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(
            enabled=True, url="http://prowlarr:9696", api_key="secret"
        )
    )
    assert prefs.is_usenet_ready() is False
    prefs.save_indexer(
        NewznabIndexerSettings(name="DS", url="https://idx.test/api", api_key="k")
    )
    assert prefs.is_usenet_ready() is True  # native row counts under "indexers"
    prefs.save_usenet_search_backend("prowlarr")
    assert prefs.is_usenet_ready() is True  # prowlarr counts under "prowlarr"
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(enabled=False, url="", api_key="")
    )
    assert prefs.is_usenet_ready() is False  # native row no longer counts


def test_readiness_masked_sentinel_must_not_read_ready(prefs):
    # The masked getter's sentinel is truthy: readiness must consult the raw key.
    _enable_sabnzbd(prefs)
    assert prefs.get_prowlarr_connection().api_key == ""
    assert prefs.is_prowlarr_configured() is False
    assert prefs.is_usenet_ready() is False


def test_readiness_requires_sabnzbd_even_with_prowlarr(prefs):
    # Prowlarr selected+configured but SABnzbd disabled: still not ready
    # (download needs SABnzbd).
    prefs.save_usenet_search_backend("prowlarr")
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(
            enabled=True, url="http://prowlarr:9696", api_key="secret"
        )
    )
    assert prefs.is_usenet_ready() is False


def test_readiness_requires_prowlarr_enabled_and_url_and_key(prefs):
    _enable_sabnzbd(prefs)
    prefs.save_usenet_search_backend("prowlarr")
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(
            enabled=False, url="http://prowlarr:9696", api_key="secret"
        )
    )
    assert prefs.is_usenet_ready() is False
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(enabled=True, url="", api_key="secret")
    )
    assert prefs.is_usenet_ready() is False
    prefs.save_prowlarr_connection(
        ProwlarrConnectionSettings(enabled=True, url="http://prowlarr:9696", api_key="")
    )
    assert prefs.is_usenet_ready() is False
