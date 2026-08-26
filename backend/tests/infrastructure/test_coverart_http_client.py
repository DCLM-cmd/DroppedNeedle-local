import pytest

from infrastructure.http.client import (
    HttpClientFactory,
    get_coverart_http_client,
    get_http_client,
)


@pytest.fixture(autouse=True)
def _isolated_factory():
    HttpClientFactory.reset_for_tests()
    yield
    HttpClientFactory.reset_for_tests()


def test_coverart_client_uses_short_budget_and_distinct_name():
    """Covers ride their own short-budget client so a slow archive.org fetch degrades to a
    placeholder instead of holding the request open, and retuning it never touches the shared
    'default' client used by MusicBrainz et al."""
    client = get_coverart_http_client()

    # Short budget: 6s read, 3s connect - not the 10s shared default.
    assert client.timeout.read == 6.0
    assert client.timeout.connect == 3.0

    # Cached under its own logical name, distinct from the default client -
    # F-PERF-08: keys carry the full effective configuration plus the name.
    assert any(
        key[1] == "coverart" and cached is client
        for key, cached in HttpClientFactory._clients.items()
    )
    default_client = get_http_client()
    assert client is not default_client

    # Construction order must not matter: cover-art keeps its short budget
    # whether it is built before or after the shared default client.
    HttpClientFactory.reset_for_tests()
    first_default = get_http_client()
    second_cover = get_coverart_http_client()
    assert second_cover.timeout.read == 6.0
    assert second_cover is not first_default
    third_cover = get_coverart_http_client()
    assert third_cover is second_cover
