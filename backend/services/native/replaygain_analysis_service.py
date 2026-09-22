"""Bounded analysis-only loudgain integration for Library Management."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import math
from pathlib import Path
import shutil
import subprocess

from models.library_management_enrichment import (
    ReplayGainAnalysis,
    ReplayGainTrackResult,
)

_MAX_ALBUM_TRACKS = 500
_MAX_ANALYSIS_SECONDS = 300

ReplayGainRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def _number(value: str, suffix: str = "") -> float:
    normalized = value.removesuffix(suffix).strip()
    result = float(normalized)
    if not math.isfinite(result):
        raise ValueError("loudgain returned a non-finite value")
    return result


def _file_state(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


async def _snapshot_file_states(sources: tuple[Path, ...]) -> tuple[tuple[int, int], ...]:
    return await asyncio.to_thread(lambda: tuple(_file_state(path) for path in sources))


def _is_timeout_error(error: BaseException) -> bool:
    return isinstance(error, (subprocess.TimeoutExpired, TimeoutError))


async def _probe_file_states(
    sources: tuple[Path, ...],
) -> tuple[dict[Path, tuple[int, int]], list[Path]]:
    def _collect() -> tuple[dict[Path, tuple[int, int]], list[Path]]:
        states: dict[Path, tuple[int, int]] = {}
        missing: list[Path] = []
        for path in sources:
            try:
                states[path] = _file_state(path)
            except OSError:
                missing.append(path)
        return states, missing

    return await asyncio.to_thread(_collect)


class ReplayGainAnalysisService:
    def __init__(
        self,
        executable: str = "loudgain",
        *,
        runner: ReplayGainRunner | None = None,
    ) -> None:
        self._executable = executable
        self._runner = runner
        self._runner_injected = runner is not None
        self._semaphore = asyncio.Semaphore(1)
        self._version: str | None = None

    async def analyze(
        self,
        paths: Sequence[Path],
        *,
        album_aware: bool,
    ) -> ReplayGainAnalysis:
        sources = tuple(paths)
        if not sources or len(sources) > _MAX_ALBUM_TRACKS:
            return ReplayGainAnalysis(
                status="deferred", reason="The ReplayGain album size is invalid."
            )
        if len(set(sources)) != len(sources) or any(
            not path.is_absolute() or "\t" in str(path) or "\n" in str(path)
            for path in sources
        ):
            return ReplayGainAnalysis(
                status="deferred", reason="A ReplayGain source path is invalid."
            )
        executable = (
            self._executable
            if self._runner_injected
            else shutil.which(self._executable)
        )
        if executable is None:
            return ReplayGainAnalysis(
                status="deferred", reason="The loudgain analyzer is unavailable."
            )
        try:
            states = await _snapshot_file_states(sources)
            before = dict(zip(sources, states))
        except OSError:
            # One unreadable track must not poison the album: keep the
            # readable subset and skip the rest individually.
            before, _ = await _probe_file_states(sources)
        candidates = tuple(path for path in sources if path in before)
        if not candidates:
            return ReplayGainAnalysis(
                status="deferred", reason="A ReplayGain source is unavailable."
            )
        timeout = min(_MAX_ANALYSIS_SECONDS, 30 + 10 * len(sources))
        command = (
            executable,
            "-a" if album_aware else "-r",
            "-s",
            "s",
            "-O",
            "-q",
            *(str(path) for path in candidates),
        )
        async with self._semaphore:
            try:
                if self._version is None:
                    version = await self._run_command((executable, "--version"), 10)
                    if version.returncode == 0:
                        self._version = (
                            version.stdout.strip() or version.stderr.strip() or None
                        )
            except (OSError, subprocess.SubprocessError, ValueError):
                return ReplayGainAnalysis(
                    status="deferred",
                    analyzer_version=self._version,
                    reason="ReplayGain analysis failed.",
                )
            deadline = asyncio.get_running_loop().time() + timeout
            analyzed: dict[str, ReplayGainTrackResult] = {}
            failure_reason = "ReplayGain analysis failed."
            try:
                completed = await self._run_with_retry(command, timeout)
            except (OSError, subprocess.SubprocessError, ValueError):
                # Only timeout-like errors escape the retry helper; a timeout
                # is album-global and defers the whole album.
                return ReplayGainAnalysis(
                    status="deferred",
                    analyzer_version=self._version,
                    reason="ReplayGain analysis failed.",
                )
            if completed is not None and completed.returncode == 0:
                try:
                    tracks = self._parse_output(
                        completed.stdout,
                        sources=candidates,
                        album_aware=album_aware,
                    )
                except (KeyError, ValueError):
                    failure_reason = "ReplayGain returned an invalid result."
                else:
                    analyzed = {track.source_path: track for track in tracks}
            if len(analyzed) < len(candidates):
                missing = tuple(
                    path for path in candidates if str(path) not in analyzed
                )
                try:
                    for track in await self._analyze_each(
                        executable, missing, deadline
                    ):
                        analyzed[track.source_path] = track
                except (OSError, subprocess.SubprocessError, ValueError):
                    return ReplayGainAnalysis(
                        status="deferred",
                        analyzer_version=self._version,
                        reason="ReplayGain analysis failed.",
                    )
        try:
            states = await _snapshot_file_states(candidates)
            after = dict(zip(candidates, states))
            after_missing: list[Path] = []
        except OSError:
            after, after_missing = await _probe_file_states(candidates)
        gone = set(after_missing)
        altered = {
            path for path in candidates if path in after and after[path] != before[path]
        }
        for path in gone | altered:
            analyzed.pop(str(path), None)
        if not analyzed:
            if len(gone | altered) == len(candidates):
                if gone:
                    return ReplayGainAnalysis(
                        status="deferred",
                        analyzer_version=self._version,
                        reason="A ReplayGain source changed during analysis.",
                    )
                return ReplayGainAnalysis(
                    status="deferred",
                    analyzer_version=self._version,
                    reason="The analyzer changed a source file.",
                )
            return ReplayGainAnalysis(
                status="deferred",
                analyzer_version=self._version,
                reason=failure_reason,
            )
        ordered = [analyzed[str(path)] for path in sources if str(path) in analyzed]
        skipped = len(sources) - len(ordered)
        return ReplayGainAnalysis(
            status="available",
            tracks=tuple(ordered),
            analyzer_version=self._version,
            reason=(
                None
                if skipped == 0
                else f"ReplayGain analysis skipped {skipped} of {len(sources)} tracks."
            ),
        )

    async def _run_with_retry(
        self, command: Sequence[str], timeout_budget: float
    ) -> subprocess.CompletedProcess[str] | None:
        """Invoke loudgain up to twice without exceeding the timeout budget.

        Returns the successful result, the last nonzero result, or None when
        the invocation itself kept failing. Only timeout-like errors escape;
        they are album-global and must defer the whole album.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_budget, 0.0)
        attempt_timeout = timeout_budget
        last_completed: subprocess.CompletedProcess[str] | None = None
        for attempt in range(2):
            try:
                completed = await self._run_command(command, attempt_timeout)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                if _is_timeout_error(error):
                    raise
                last_completed = None
            else:
                if completed.returncode == 0:
                    return completed
                last_completed = completed
            if attempt == 0:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                attempt_timeout = remaining
        return last_completed

    async def _analyze_each(
        self,
        executable: str,
        tracks: Sequence[Path],
        deadline: float,
    ) -> list[ReplayGainTrackResult]:
        """Analyze tracks one by one, skipping individual failures."""
        results: list[ReplayGainTrackResult] = []
        loop = asyncio.get_running_loop()
        for path in tracks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            completed = await self._run_with_retry(
                (executable, "-r", "-s", "s", "-O", "-q", str(path)),
                min(remaining, 30 + 10 * 1),
            )
            if completed is None or completed.returncode != 0:
                continue
            try:
                (track,) = self._parse_output(
                    completed.stdout, sources=(path,), album_aware=False
                )
            except (KeyError, ValueError):
                continue
            results.append(track)
        return results

    async def _run_command(
        self, command: Sequence[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        if self._runner is not None:
            return await asyncio.to_thread(self._runner, command, timeout_seconds)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.CancelledError:
            await self._finish_process_cleanup(process)
            raise
        except TimeoutError as error:
            await self._finish_process_cleanup(process)
            raise subprocess.TimeoutExpired(command, timeout_seconds) from error
        assert process.returncode is not None
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    @classmethod
    async def _finish_process_cleanup(cls, process: asyncio.subprocess.Process) -> None:
        cleanup = asyncio.create_task(cls._terminate_process(process))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _parse_output(
        output: str,
        *,
        sources: tuple[Path, ...],
        album_aware: bool,
    ) -> tuple[ReplayGainTrackResult, ...]:
        lines = [line for line in output.splitlines() if line]
        header_index = next(
            (index for index, line in enumerate(lines) if line.startswith("File\t")),
            None,
        )
        if header_index is None:
            raise ValueError("loudgain did not return a header")
        headers = lines[header_index].split("\t")
        gain_index = headers.index("Gain")
        peak_index = headers.index("True_Peak")
        by_path: dict[str, tuple[float, float]] = {}
        album: tuple[float, float] | None = None
        for line in lines[header_index + 1 :]:
            columns = line.split("\t")
            if len(columns) != len(headers):
                raise ValueError("unexpected loudgain column count")
            gain = _number(columns[gain_index], "dB")
            peak = _number(columns[peak_index])
            if peak < 0:
                raise ValueError("negative ReplayGain peak")
            if columns[0] == "Album":
                album = (gain, peak)
            else:
                by_path[columns[0]] = (gain, peak)
        if set(by_path) != {str(path) for path in sources}:
            raise ValueError("loudgain did not return every source")
        if album_aware and album is None:
            raise ValueError("loudgain did not return album values")
        return tuple(
            ReplayGainTrackResult(
                source_path=str(path),
                track_gain_db=by_path[str(path)][0],
                track_peak=by_path[str(path)][1],
                album_gain_db=album[0] if album is not None else None,
                album_peak=album[1] if album is not None else None,
            )
            for path in sources
        )
