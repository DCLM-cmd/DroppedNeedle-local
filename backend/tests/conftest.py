import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ROOT_APP_DIR", tempfile.mkdtemp())

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.crypto import init_crypto

init_crypto(Path(os.environ["ROOT_APP_DIR"]) / "config")


_MB_SOURCE_GLOBALS = (
    "_mb_api_base",
    "_mb_source_generation",
    "_mb_source_mode",
    "_mb_source_id",
    "_brainzmash_runtime_enabled",
    "_mb_limiter_bypassed",
)


@pytest.fixture(autouse=True)
def _restore_musicbrainz_source():
    """Keep the process-wide MusicBrainz source binding test-local.

    Any test that builds the real app lets MusicBrainzRepository apply the
    configured source (e.g. BrainzMash), and nothing put it back. A later test
    that only flipped the BrainzMash runtime flag then left every captured
    source context stale, so ~200 unrelated MusicBrainz tests failed with
    "source changed" - but only in a full run, never in isolation.
    """
    import repositories.musicbrainz_base as mb_base

    saved = {name: getattr(mb_base, name) for name in _MB_SOURCE_GLOBALS}
    yield
    for name, value in saved.items():
        setattr(mb_base, name, value)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: end-to-end test that may require external services (e.g. a real slskd container)",
    )
