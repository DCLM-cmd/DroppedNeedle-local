"""AudioFingerprinter - the invalid-API-key path must be loud and unambiguous.

A wrong AcoustID key means every verification silently degrades to unverified
imports (the 2026-07-17 misassignment incident): the fingerprinter must surface
that as ONE actionable ERROR log, not a per-file WARNING storm."""

import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from infrastructure.audio.fingerprinter import AudioFingerprinter, FingerprintStatus


class _NoWaitLimiter:
    async def acquire(self) -> None:
        return None


def _fingerprinter(handler) -> AudioFingerprinter:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fp = AudioFingerprinter(
        http=http, api_key_provider=lambda: "some-key", rate_limiter=_NoWaitLimiter()
    )
    # fpcalc isn't available/needed in tests - return a canned fingerprint
    fp._run_fpcalc = AsyncMock(return_value=("AQAD_fake", 184))
    return fp


@pytest.mark.asyncio
async def test_invalid_api_key_logs_one_error_and_returns_error(tmp_path, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": 4, "message": "invalid API key"}, "status": "error"}
        )

    fp = _fingerprinter(handler)
    with caplog.at_level(logging.ERROR, logger="infrastructure.audio.fingerprinter"):
        first = await fp.fingerprint(tmp_path / "a.flac")
        second = await fp.fingerprint(tmp_path / "b.flac")

    assert first.status == FingerprintStatus.ERROR
    assert first.error == "invalid AcoustID API key"
    assert second.status == FingerprintStatus.ERROR
    key_errors = [r for r in caplog.records if "rejected the configured API key" in r.message]
    assert len(key_errors) == 1  # loud once, not per file


@pytest.mark.asyncio
async def test_other_400s_stay_per_file_warnings(tmp_path, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": 3, "message": "invalid fingerprint"}, "status": "error"}
        )

    fp = _fingerprinter(handler)
    with caplog.at_level(logging.WARNING, logger="infrastructure.audio.fingerprinter"):
        result = await fp.fingerprint(tmp_path / "a.flac")

    assert result.status == FingerprintStatus.ERROR
    assert "invalid AcoustID API key" not in (result.error or "")
    assert not any("rejected the configured API key" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_valid_lookup_still_passes(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "ok",
            "results": [{
                "score": 0.95,
                "recordings": [{
                    "id": "rec-1", "title": "Song", "duration": 184,
                    "artists": [{"name": "Artist"}],
                    "releasegroups": [{"id": "rg-1"}],
                }],
            }],
        })

    fp = _fingerprinter(handler)
    result = await fp.fingerprint(tmp_path / "a.flac")

    assert result.status == FingerprintStatus.PASS
    assert result.recording_id == "rec-1"
    assert result.release_group_ids == ["rg-1"]
