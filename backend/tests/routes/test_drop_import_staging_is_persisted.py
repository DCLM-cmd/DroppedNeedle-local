"""Files awaiting manual matching must outlive a container replacement.

create_job MOVES the upload into staging, so what sits there is the only copy. The
staging root was under root_app_dir, which is the image's writable layer and not one
of the mounted volumes - so every update destroyed 245 files across 16 albums that
were waiting to be matched by hand, with nothing to indicate they had gone.
"""

from core.config import get_settings


def test_the_configured_staging_root_is_inside_cache_dir():
    from core.dependencies.service_providers import get_drop_import_service

    service = get_drop_import_service()
    settings = get_settings()

    assert settings.cache_dir in service._staging_root.parents or (
        service._staging_root.parent == settings.cache_dir
    )
