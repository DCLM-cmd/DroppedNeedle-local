import asyncio
import os
from pathlib import Path
import shutil

from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, SYLT, USLT
import pytest

from services.compat.native_lyrics_service import NativeLyricsService


class _LocalFiles:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def resolve_validated_path(self, file_id: str) -> Path:
        assert file_id == "track-1"
        return self.path


@pytest.mark.asyncio
async def test_reads_synchronized_vorbis_comment_written_for_flac(tmp_path):
    source = Path(__file__).parents[1] / "fixtures" / "library" / "management_full.flac"
    audio = tmp_path / "song.flac"
    shutil.copy2(source, audio)
    tags = FLAC(audio)
    tags["SYNCEDLYRICS"] = ["[00:02.500]second\n[00:01.000]first"]
    tags.save()

    result = await NativeLyricsService(_LocalFiles(audio)).get("track-1")

    assert result is not None
    assert result.synced is True
    assert [(line.start_ms, line.value) for line in result.lines] == [
        (1000, "first"),
        (2500, "second"),
    ]


@pytest.mark.asyncio
async def test_prefers_synchronized_id3_lyrics_when_plain_lyrics_are_also_present(
    tmp_path,
):
    source = Path(__file__).parents[1] / "fixtures" / "library" / "management_full.mp3"
    audio = tmp_path / "song.mp3"
    shutil.copy2(source, audio)
    tags = ID3(audio)
    tags.delall("USLT")
    tags.delall("SYLT")
    tags.add(USLT(encoding=3, lang="eng", desc="", text="plain lyrics"))
    tags.add(
        SYLT(
            encoding=3,
            lang="eng",
            format=2,
            type=1,
            desc="",
            text=[("first", 1000), ("second", 2500)],
        )
    )
    tags.save(audio)

    result = await NativeLyricsService(_LocalFiles(audio)).get("track-1")

    assert result is not None
    assert result.synced is True
    assert [(line.start_ms, line.value) for line in result.lines] == [
        (1000, "first"),
        (2500, "second"),
    ]


@pytest.mark.asyncio
async def test_ignores_non_lyric_or_non_millisecond_sylt_frames(tmp_path):
    source = Path(__file__).parents[1] / "fixtures" / "library" / "management_full.mp3"
    audio = tmp_path / "song.mp3"
    shutil.copy2(source, audio)
    tags = ID3(audio)
    tags.delall("SYLT")
    tags.add(
        SYLT(
            encoding=3,
            lang="eng",
            format=1,
            type=2,
            desc="",
            text=[("not millisecond lyrics", 9)],
        )
    )
    tags.add(
        SYLT(
            encoding=3,
            lang="eng",
            format=2,
            type=1,
            desc="managed",
            text=[("first", 1000), ("second", 2500)],
        )
    )
    tags.save(audio)

    result = await NativeLyricsService(_LocalFiles(audio)).get("track-1")

    assert result is not None
    assert result.synced is True
    assert [(line.start_ms, line.value) for line in result.lines] == [
        (1000, "first"),
        (2500, "second"),
    ]


@pytest.mark.asyncio
async def test_reads_wma_synchronized_lyrics_before_plain_fallback(tmp_path):
    source = Path(__file__).parents[1] / "fixtures" / "library" / "management_full.wma"
    audio = tmp_path / "song.wma"
    shutil.copy2(source, audio)
    tags = ASF(audio)
    tags["WM/Lyrics"] = ["plain lyrics"]
    tags["WM/Lyrics_Synchronised"] = ["[00:01.000]first\n[00:02.500]second"]
    tags.save()

    result = await NativeLyricsService(_LocalFiles(audio)).get("track-1")

    assert result is not None
    assert result.synced is True
    assert [(line.start_ms, line.value) for line in result.lines] == [
        (1000, "first"),
        (2500, "second"),
    ]


@pytest.mark.asyncio
async def test_reads_wma_plain_lyrics_when_synchronized_lyrics_are_absent(tmp_path):
    source = Path(__file__).parents[1] / "fixtures" / "library" / "management_full.wma"
    audio = tmp_path / "song.wma"
    shutil.copy2(source, audio)
    tags = ASF(audio)
    tags["WM/Lyrics"] = ["first\nsecond"]
    tags.pop("WM/Lyrics_Synchronised", None)
    tags.save()

    result = await NativeLyricsService(_LocalFiles(audio)).get("track-1")

    assert result is not None
    assert result.synced is False
    assert [line.value for line in result.lines] == ["first", "second"]


@pytest.mark.asyncio
async def test_reads_sorts_and_bounds_lrc_sidecar_off_thread(tmp_path):
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"not-read-when-sidecar-exists")
    audio.with_suffix(".lrc").write_text(
        "[00:02.50]second\n[00:01.005]first\n[00:02.50]layer",
        encoding="utf-8",
    )

    result = await NativeLyricsService(_LocalFiles(audio)).get("track-1")

    assert result is not None
    assert result.synced is True
    assert [(line.start_ms, line.value) for line in result.lines] == [
        (1005, "first"),
        (2500, "second"),
        (2500, "layer"),
    ]


@pytest.mark.asyncio
async def test_plain_lrc_is_unsynced_and_cache_tracks_sidecar_identity(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"not-read-when-sidecar-exists")
    sidecar = audio.with_suffix(".lrc")
    sidecar.write_text("one\ntwo", encoding="utf-8")
    service = NativeLyricsService(_LocalFiles(audio))

    first = await service.get("track-1")
    sidecar.write_text("changed", encoding="utf-8")
    os.utime(sidecar, ns=(sidecar.stat().st_atime_ns, sidecar.stat().st_mtime_ns + 1))
    second = await service.get("track-1")

    assert first is not None and first.synced is False
    assert [line.value for line in first.lines] == ["one", "two"]
    assert second is not None
    assert [line.value for line in second.lines] == ["changed"]


@pytest.mark.asyncio
async def test_embedded_cache_tracks_changes_when_size_and_mtime_are_preserved(
    tmp_path,
):
    source = Path(__file__).parents[1] / "fixtures" / "library" / "management_full.flac"
    audio = tmp_path / "song.flac"
    shutil.copy2(source, audio)
    tags = FLAC(audio)
    tags["LYRICS"] = ["one"]
    tags.save()
    before = audio.stat()
    service = NativeLyricsService(_LocalFiles(audio))

    first = await service.get("track-1")
    await asyncio.sleep(0.01)
    tags = FLAC(audio)
    tags["LYRICS"] = ["two"]
    tags.save()
    os.utime(audio, ns=(audio.stat().st_atime_ns, before.st_mtime_ns))
    after = audio.stat()
    second = await service.get("track-1")

    assert before.st_size == after.st_size
    assert before.st_mtime_ns == after.st_mtime_ns
    assert first is not None
    assert [line.value for line in first.lines] == ["one"]
    assert second is not None
    assert [line.value for line in second.lines] == ["two"]


@pytest.mark.asyncio
async def test_sidecar_cache_tracks_changes_when_size_and_mtime_are_preserved(tmp_path):
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"not-read-when-sidecar-exists")
    sidecar = audio.with_suffix(".lrc")
    sidecar.write_text("one", encoding="utf-8")
    before = sidecar.stat()
    service = NativeLyricsService(_LocalFiles(audio))

    first = await service.get("track-1")
    await asyncio.sleep(0.01)
    sidecar.write_text("two", encoding="utf-8")
    os.utime(sidecar, ns=(sidecar.stat().st_atime_ns, before.st_mtime_ns))
    after = sidecar.stat()
    second = await service.get("track-1")

    assert before.st_size == after.st_size
    assert before.st_mtime_ns == after.st_mtime_ns
    assert first is not None
    assert [line.value for line in first.lines] == ["one"]
    assert second is not None
    assert [line.value for line in second.lines] == ["two"]


@pytest.mark.asyncio
async def test_oversized_sidecar_degrades_to_absence(tmp_path):
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"audio")
    audio.with_suffix(".lrc").write_bytes(b"x" * (1_048_576 + 1))

    assert await NativeLyricsService(_LocalFiles(audio)).get("track-1") is None
