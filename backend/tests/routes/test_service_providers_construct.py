"""Every download-service provider must actually BUILD.

A missing local import or a kwarg the builder does not accept raises only when the
provider is CALLED - importing the module, or the router that depends on it, proves
nothing. That gap shipped a NameError to production: the Downloads page answered
"couldn't load downloads" for every request while the whole test suite stayed green,
because the tests construct DownloadService directly and never go through the
composition root.
"""

import pytest


@pytest.mark.parametrize(
    "provider_name",
    ["get_download_service", "get_target_download_service"],
)
def test_the_provider_builds(provider_name):
    from core.dependencies import service_providers

    service = getattr(service_providers, provider_name)()

    assert service is not None
    # the dependency this file exists to protect: added for replacing an occupied
    # import destination, and reachable only through the composition root
    assert hasattr(service, "_native_library_store")


def test_the_built_service_can_answer_a_list_call():
    """Construction alone is not proof: the page failed inside the request, not at
    startup. Reaching a real method is what the user's first click does."""
    from core.dependencies import service_providers

    service = service_providers.get_target_download_service()

    assert callable(service.list_tasks)
