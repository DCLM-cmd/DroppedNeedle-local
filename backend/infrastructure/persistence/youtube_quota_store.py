import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import msgspec

from core.exceptions import ExternalServiceError, RateLimitedError


class YouTubeQuotaState(msgspec.Struct):
    date: str = ""
    count: int = 0


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class YouTubeQuotaStore:
    """Shared by repositories using the same instance quota file."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        try:
            self.state = msgspec.json.decode(path.read_bytes(), type=YouTubeQuotaState)
        except FileNotFoundError:
            self.state = YouTubeQuotaState()
        except (OSError, msgspec.DecodeError) as exc:
            raise ExternalServiceError("Could not load YouTube quota") from exc
        if self.state.count < 0:
            raise ExternalServiceError("Invalid YouTube quota count")

    @property
    def count(self) -> int:
        return self.state.count if self.state.date == utc_today() else 0

    def _write(self, state: YouTubeQuotaState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".youtube-quota-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(msgspec.json.encode(state))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.state = state
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def _settle(self, state: YouTubeQuotaState) -> None:
        task = asyncio.create_task(asyncio.to_thread(self._write, state))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except OSError:
                break
        try:
            task.result()
        except OSError as exc:
            raise ExternalServiceError("Could not persist YouTube quota") from exc
        if cancelled:
            raise asyncio.CancelledError

    async def reserve(self, limit: int) -> str:
        async with self.lock:
            date = utc_today()
            count = self.count
            if count >= limit:
                raise RateLimitedError(
                    "YouTube daily quota exceeded",
                    details={"reason": "youtube_daily_quota"},
                )
            try:
                await self._settle(YouTubeQuotaState(date=date, count=count + 1))
            except asyncio.CancelledError:
                await self._settle(YouTubeQuotaState(date=date, count=count))
                raise
            return date

    async def refund(self, date: str) -> None:
        async with self.lock:
            if self.state.date == date:
                await self._settle(YouTubeQuotaState(date=date, count=max(0, self.state.count - 1)))
