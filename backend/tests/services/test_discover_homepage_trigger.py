"""F-15: homepage triggered-warmer must log failures via done-callback."""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from core.task_registry import TaskRegistry
from services.discover.homepage_service import DiscoverHomepageService


def _service() -> DiscoverHomepageService:
    return DiscoverHomepageService(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()
    )


@pytest.mark.asyncio
async def test_triggered_warm_failure_is_logged(monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("warm boom")

    monkeypatch.setattr(DiscoverHomepageService, "_run_triggered_warm", boom)
    user_id = "user-f15-probe"
    name = f"discover-homepage-warm-{user_id}"
    TaskRegistry.get_instance().unregister(name)
    try:
        with caplog.at_level(
            logging.ERROR, logger="services.discover.homepage_service"
        ):
            _service()._trigger_warm(user_id)
            for _ in range(100):
                await asyncio.sleep(0.01)
                if not TaskRegistry.get_instance().is_running(name):
                    break
        assert f"Triggered homepage warm failed user={user_id}" in caplog.text
    finally:
        TaskRegistry.get_instance().unregister(name)
