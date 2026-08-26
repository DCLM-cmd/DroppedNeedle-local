import asyncio
import hashlib
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock

import pytest

from services.native.replaygain_analysis_service import ReplayGainAnalysisService


def _runner(output: str, *, returncode: int = 0):
    def run(command, _timeout):  # noqa: ANN001
        if "--version" in command:
            return subprocess.CompletedProcess(
                command, 0, stdout="loudgain 0.6.8\n", stderr=""
            )
        return subprocess.CompletedProcess(
            command, returncode, stdout=output, stderr=""
        )

    return run


def _output(first: Path, second: Path) -> str:
    header = (
        "File\tLoudness\tRange\tTrue_Peak\tTrue_Peak_dBTP\tReference\t"
        "Will_clip\tClip_prevent\tGain\tNew_Peak\tNew_Peak_dBTP"
    )
    return "\n".join(
        (
            header,
            f"{first}\t-21.75 LUFS\t0.00 dB\t0.125093\t-18.06 dBTP\t"
            "-18.00 LUFS\tN\tN\t3.75 dB\t0.192705\t-14.30 dBTP",
            f"{second}\t-27.33 LUFS\t0.00 dB\t0.062546\t-24.08 dBTP\t"
            "-18.00 LUFS\tN\tN\t9.33 dB\t0.183022\t-14.75 dBTP",
            "Album\t-23.70 LUFS\t0.00 dB\t0.125093\t-18.06 dBTP\t"
            "-18.00 LUFS\tN\tN\t5.70 dB\t0.241150\t-12.35 dBTP",
        )
    )


@pytest.mark.asyncio
async def test_album_analysis_parses_track_and_album_values_without_source_mutation(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.flac"
    second = tmp_path / "second.flac"
    first.write_bytes(b"first audio")
    second.write_bytes(b"second audio")
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)
    }
    service = ReplayGainAnalysisService(
        executable="/usr/bin/loudgain",
        runner=_runner(_output(first, second)),
    )

    result = await service.analyze((first, second), album_aware=True)

    assert result.status == "available"
    assert result.analyzer_version == "loudgain 0.6.8"
    assert result.tracks[0].track_gain_db == 3.75
    assert result.tracks[0].track_peak == 0.125093
    assert result.tracks[0].album_gain_db == 5.7
    assert result.tracks[0].album_peak == 0.125093
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)
    } == before


@pytest.mark.asyncio
async def test_non_finite_or_failed_analysis_is_deferred(tmp_path: Path) -> None:
    source = tmp_path / "silent.flac"
    source.write_bytes(b"audio")
    invalid = _output(source, source).replace("3.75 dB", "inf dB", 1)

    non_finite = await ReplayGainAnalysisService(
        executable="/usr/bin/loudgain", runner=_runner(invalid)
    ).analyze((source,), album_aware=True)
    failed = await ReplayGainAnalysisService(
        executable="/usr/bin/loudgain", runner=_runner("", returncode=1)
    ).analyze((source,), album_aware=False)
    malformed = await ReplayGainAnalysisService(
        executable="/usr/bin/loudgain", runner=_runner("not tabular output")
    ).analyze((source,), album_aware=False)

    assert non_finite.status == "deferred"
    assert failed.status == "deferred"
    assert malformed.status == "deferred"


@pytest.mark.asyncio
async def test_analyzer_source_change_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "track.flac"
    source.write_bytes(b"audio")

    def mutating_runner(command, _timeout):  # noqa: ANN001
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="0.6.8", stderr="")
        source.write_bytes(b"changed")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = await ReplayGainAnalysisService(
        executable="/usr/bin/loudgain", runner=mutating_runner
    ).analyze((source,), album_aware=False)

    assert result.status == "deferred"
    assert result.reason == "The analyzer changed a source file."


@pytest.mark.asyncio
async def test_cancellation_reaps_analyzer_before_releasing_concurrency_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "track.flac"
    source.write_bytes(b"audio")
    started = asyncio.Event()
    terminated = asyncio.Event()
    allow_exit = asyncio.Event()

    class VersionProcess:
        returncode = 0

        async def communicate(self):
            return b"loudgain 0.6.8\n", b""

    class AnalysisProcess:
        returncode = None

        async def communicate(self):
            started.set()
            await asyncio.Future()

        def terminate(self) -> None:
            terminated.set()

        def kill(self) -> None:
            self.returncode = -9
            allow_exit.set()

        async def wait(self) -> int:
            await allow_exit.wait()
            self.returncode = -15
            return self.returncode

    analysis = AnalysisProcess()
    create_process = AsyncMock(side_effect=[VersionProcess(), analysis])
    monkeypatch.setattr(
        "services.native.replaygain_analysis_service.shutil.which",
        lambda _value: "/usr/bin/loudgain",
    )
    monkeypatch.setattr(
        "services.native.replaygain_analysis_service.asyncio.create_subprocess_exec",
        create_process,
    )
    service = ReplayGainAnalysisService()

    task = asyncio.create_task(service.analyze((source,), album_aware=False))
    await started.wait()
    task.cancel()
    await terminated.wait()

    assert service._semaphore.locked()
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert service._semaphore.locked()
    assert not task.done()
    allow_exit.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert analysis.returncode == -15
    assert not service._semaphore.locked()

@pytest.mark.asyncio
async def test_heartbeat_advances_while_blocked_stat_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    first = tmp_path / "a.flac"
    second = tmp_path / "b.flac"
    first.write_bytes(b"audio")
    second.write_bytes(b"audio")

    def slow_stat(path: Path):
        _time.sleep(0.15)
        return (123, 456)

    monkeypatch.setattr("services.native.replaygain_analysis_service._file_state", slow_stat)
    monkeypatch.setattr("services.native.replaygain_analysis_service.shutil.which", lambda _v: "/usr/bin/loudgain")

    async def fake_run(cmd, timeout):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="loudgain 0.6.8\n", stderr="")
        n = max(1, len(cmd) - 6)
        header = "File\tLoudness\tRange\tTrue_Peak\tTrue_Peak_dBTP\tReference\tWill_clip\tClip_prevent\tGain\tNew_Peak\tNew_Peak_dBTP"
        lines = [header]
        # cmd[6:] are the source paths
        for path_str in cmd[6:]:
            lines.append(f"{path_str}\t-21.75 LUFS\t0.00 dB\t0.125093\t-18.06 dBTP\t-18.00 LUFS\tN\tN\t3.75 dB\t0.192705\t-14.30 dBTP")
        lines.append("Album\t-23.70 LUFS\t0.00 dB\t0.125093\t-18.06 dBTP\t-18.00 LUFS\tN\tN\t5.70 dB\t0.241150\t-12.35 dBTP")
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(lines) + "\n", stderr="")

    monkeypatch.setattr("services.native.replaygain_analysis_service.ReplayGainAnalysisService._run_command", AsyncMock(side_effect=fake_run))
    service = ReplayGainAnalysisService()
    heartbeat = 0

    async def beep():
        nonlocal heartbeat
        while True:
            heartbeat += 1
            await asyncio.sleep(0.02)

    beeper = asyncio.create_task(beep())
    try:
        result = await service.analyze((first, second), album_aware=True)
        print(f"heartbeat {heartbeat} result {result} reason {getattr(result, 'reason', None)}")
        assert heartbeat >= 2, f"heartbeat {heartbeat} should advance while stat blocked"
        assert result.status == "available", f"got {result} reason {getattr(result, 'reason', None)}"
    finally:
        beeper.cancel()
        try:
            await beeper
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_exactly_two_complete_batches_including_500_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.native.replaygain_analysis_service.shutil.which", lambda _v: "/usr/bin/loudgain")
    calls: list[tuple] = []
    orig_to_thread = asyncio.to_thread

    async def counting_to_thread(func, *args, **kwargs):
        calls.append((func, args))
        return await orig_to_thread(func, *args, **kwargs)

    async def fake_run(cmd, timeout):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="loudgain 0.6.8\n", stderr="")
        header = "File\tLoudness\tRange\tTrue_Peak\tTrue_Peak_dBTP\tReference\tWill_clip\tClip_prevent\tGain\tNew_Peak\tNew_Peak_dBTP"
        lines = [header]
        for path_str in cmd[6:]:
            lines.append(f"{path_str}\t-21.75 LUFS\t0.00 dB\t0.125093\t-18.06 dBTP\t-18.00 LUFS\tN\tN\t3.75 dB\t0.192705\t-14.30 dBTP")
        lines.append("Album\t-23.70 LUFS\t0.00 dB\t0.125093\t-18.06 dBTP\t-18.00 LUFS\tN\tN\t5.70 dB\t0.241150\t-12.35 dBTP")
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(lines) + "\n", stderr="")



    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)


    monkeypatch.setattr("services.native.replaygain_analysis_service.ReplayGainAnalysisService._run_command", AsyncMock(side_effect=fake_run))
    service = ReplayGainAnalysisService()
    # 1 track -> 2 batches (before+after)
    p1 = tmp_path / "t1.flac"
    p1.write_bytes(b"a")
    calls.clear()
    await service.analyze((p1,), album_aware=False)
    assert len([c for c in calls if "tuple" in str(c[0])]) == 2 or len(calls) == 2, f"expected 2 snapshot batches, got {calls}"
    # 500 tracks -> still 2 batches, not 500 or 1000
    many = []
    for i in range(500):
        p = tmp_path / f"m{i}.flac"
        p.write_bytes(b"a")
        many.append(p)
    calls.clear()
    result = await service.analyze(tuple(many), album_aware=True)
    assert result.status == "available"
    assert len(calls) == 2, f"500 should be 2 batches, got {len(calls)}"
    # 501 -> deferred before any stat, zero batches
    over = many + [tmp_path / "extra.flac"]
    over[-1].write_bytes(b"a")
    calls.clear()
    result2 = await service.analyze(tuple(over), album_aware=True)
    assert result2.status == "deferred"
    assert result2.reason == "The ReplayGain album size is invalid."
    assert len(calls) == 0, f"501 should be 0 batches, got {len(calls)}"


@pytest.mark.asyncio
async def test_missing_source_still_deferred_with_offload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.native.replaygain_analysis_service.shutil.which", lambda _v: "/usr/bin/loudgain")
    service = ReplayGainAnalysisService()
    missing = tmp_path / "missing.flac"
    result = await service.analyze((missing,), album_aware=False)
    assert result.status == "deferred"
    assert result.reason == "A ReplayGain source is unavailable."


@pytest.mark.asyncio
async def test_cancellation_reaps_before_release_with_offload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure offload does not swallow CancelledError and semaphore released only after cleanup
    source = tmp_path / "track.flac"
    source.write_bytes(b"audio")
    monkeypatch.setattr("services.native.replaygain_analysis_service.shutil.which", lambda _v: "/usr/bin/loudgain")
    # Make _snapshot_file_states slow so cancellation during loudgain still proves semaphore held
    orig_snapshot = __import__("services.native.replaygain_analysis_service", fromlist=["_snapshot_file_states"])._snapshot_file_states

    async def slow_snapshot(sources):
        await asyncio.sleep(0.05)
        return await orig_snapshot(sources)

    monkeypatch.setattr("services.native.replaygain_analysis_service._snapshot_file_states", slow_snapshot)
    # Reuse existing cancellation test logic but ensure it still passes
    await test_cancellation_reaps_analyzer_before_releasing_concurrency_slot(tmp_path, monkeypatch)
