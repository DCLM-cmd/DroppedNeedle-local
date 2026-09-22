"""F-14: lifecycle startup tasks must log failures via done-callback."""

import asyncio
import logging

import pytest

from core.task_registry import TaskRegistry
from services.native.target_application_lifecycle import _register_task


@pytest.mark.asyncio
async def test_register_task_logs_startup_task_failure(caplog):
    async def boom():
        raise RuntimeError("startup boom")

    name = "test-register-task-failure-probe"
    registry = TaskRegistry.get_instance()
    registry.unregister(name)
    try:
        with caplog.at_level(
            logging.ERROR, logger="services.native.target_application_lifecycle"
        ):
            _register_task(name, boom())
            for _ in range(100):
                await asyncio.sleep(0.01)
                if not registry.is_running(name):
                    break
        assert f"Startup task {name} failed" in caplog.text
    finally:
        registry.unregister(name)


@pytest.mark.asyncio
async def test_register_task_ignores_cancelled_tasks(caplog):
    started = asyncio.Event()

    async def sleepy():
        started.set()
        await asyncio.sleep(60)

    name = "test-register-task-cancel-probe"
    registry = TaskRegistry.get_instance()
    registry.unregister(name)
    try:
        with caplog.at_level(
            logging.ERROR, logger="services.native.target_application_lifecycle"
        ):
            _register_task(name, sleepy())
            await asyncio.wait_for(started.wait(), timeout=5)
            await registry.cancel(name)
        assert f"Startup task {name} failed" not in caplog.text
    finally:
        registry.unregister(name)
