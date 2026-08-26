import json
from types import SimpleNamespace

from services.preferences_service import PreferencesService


def test_get_it_settings_normalization_removes_retired_fields_idempotently(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "get_it": {
                    "store_region": "gb",
                    "retired_toggle": True,
                    "unused": "value",
                }
            }
        )
    )
    settings = SimpleNamespace(config_file_path=config_path)

    first = PreferencesService(settings)
    first_config = json.loads(config_path.read_text())
    second = PreferencesService(settings)
    second_config = json.loads(config_path.read_text())

    assert first.get_get_it_settings().store_region == "GB"
    assert second.get_get_it_settings().store_region == "GB"
    assert first_config["get_it"] == {"store_region": "GB"}
    assert second_config == first_config
