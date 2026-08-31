"""The post-scan tidy-up must run when a scan completes - and never break one.

Upstream removed the legacy LibraryScanner this hook used to hang on when it moved
to the target-only runtime, so the hook lives on the scan coordinator now. These
pin the two properties that matter: it fires at completion, and a failure inside it
leaves the finished scan finished.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.native.library_scan_coordinator import LibraryScanCoordinator


def _coordinator(housekeeping):
    return LibraryScanCoordinator(
        store=AsyncMock(),
        inventory=AsyncMock(),
        indexer=AsyncMock(),
        reconciler=AsyncMock(),
        resolver_getter=lambda: SimpleNamespace(),
        housekeeping=housekeeping,
    )


@pytest.mark.asyncio
async def test_completion_runs_the_housekeeping_passes():
    housekeeping = SimpleNamespace(
        run_after_scan=AsyncMock(return_value={"deduplicated": 2, "merged": 1})
    )

    await _coordinator(housekeeping)._run_housekeeping("run-1")

    housekeeping.run_after_scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_housekeeping_failure_does_not_propagate(caplog):
    """The scan has already been published as completed by this point: raising here
    would turn a good scan into a failed one."""
    housekeeping = SimpleNamespace(
        run_after_scan=AsyncMock(side_effect=RuntimeError("bin unreachable"))
    )

    await _coordinator(housekeeping)._run_housekeeping("run-1")

    assert any("housekeeping" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_housekeeping_configured_is_a_no_op():
    await _coordinator(None)._run_housekeeping("run-1")
