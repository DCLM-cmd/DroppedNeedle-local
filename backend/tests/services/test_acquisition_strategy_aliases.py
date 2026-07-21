"""SoulseekStrategy: MusicBrainz-alias search fallback + undelivered-file quarantine.

Alias fallback: when the primary artist name yields nothing pickable (no auto/manual
candidate), the strategy re-searches under the artist's MusicBrainz aliases and scores
each pass against the ALIAS as the target artist (so artist-evidence judges the name
the peers actually share under).

Quarantine: a peer whose transfers failed (terminal) or that we abandoned (stalled /
queued-timeout / no-show) gets its UNDELIVERED files quarantined
(reason='download_failed', TTL'd) so the next search/auto-retry picks a different
source instead of re-grabbing the same dead release; delivered files are never touched.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.download import DownloadTask, ScoredCandidate
from models.download_identity import soulseek_identity
from models.download_manifest import DownloadManifest, ExpectedFile, ManifestCodec
from repositories.protocols.download_client import DownloadSearchResult, TaskHandle
from services.native.acquisition.strategy import SoulseekStrategy


def _search_result(username="peer", filename="peer/01.flac", duration=200.0):
    return DownloadSearchResult(
        username=username, filename=filename, parent_directory="peer",
        size=100, extension="flac", duration=duration,
    )


def _album_task(**overrides) -> DownloadTask:
    kwargs = dict(
        id="t1", user_id="u1", download_type="album", release_group_mbid="rg-1",
        artist_mbid="mbid-artist-1", artist_name="宇多田ヒカル", album_title="First Love",
        year=1999, track_count=12, origin="user", source_username="peer",
    )
    kwargs.update(overrides)
    return DownloadTask(**kwargs)


def _candidate(tier="auto", score=0.9):
    f = _search_result()
    return ScoredCandidate(
        username="peer", parent_directory="peer", files=[f],
        coherence=score, file_confidence=score, final_score=score, tier=tier,
    )


def _strategy(tmp_path: Path, *, aliases=None, alias_error=False):
    indexer = MagicMock()
    indexer.search_album = AsyncMock(
        return_value=[SimpleNamespace(soulseek=_search_result())]
    )
    indexer.search_track = AsyncMock(return_value=[])
    scorer = MagicMock()
    scorer.rank = AsyncMock(return_value=[])
    track_matcher = MagicMock()
    track_matcher.rank = AsyncMock(return_value=[])
    if alias_error:
        alias_resolver = AsyncMock(side_effect=RuntimeError("MB down"))
    elif aliases is None:
        alias_resolver = None
    else:
        alias_resolver = AsyncMock(return_value=aliases)
    store = AsyncMock()
    strategy = SoulseekStrategy(
        indexer=indexer, scorer=scorer, track_matcher=track_matcher,
        client=AsyncMock(), store=store, file_processor=MagicMock(),
        staging=tmp_path, manifest_codec=ManifestCodec(),
        naming_template="{albumartist}/{album}/{title}.{ext}",
        alias_resolver=alias_resolver,
    )
    return strategy, indexer, scorer, store, alias_resolver


# --- alias search fallback --------------------------------------------------------


@pytest.mark.asyncio
async def test_alias_fallback_finds_candidates_under_alias(tmp_path: Path):
    strategy, indexer, scorer, _store, resolver = _strategy(
        tmp_path, aliases=["Utada Hikaru"]
    )
    # primary name scores nothing; the alias pass finds an auto candidate
    scorer.rank = AsyncMock(side_effect=[[], [_candidate("auto")]])

    ranked = await strategy.search_and_score(_album_task(), timeout=30, auto=0.7, manual=0.5)

    assert [c.tier for c in ranked] == ["auto"]
    resolver.assert_awaited_once_with("mbid-artist-1")
    # the second search + its scoring target both use the ALIAS
    assert indexer.search_album.await_args_list[0].args[0] == "宇多田ヒカル"
    assert indexer.search_album.await_args_list[1].args[0] == "Utada Hikaru"
    alias_target = scorer.rank.await_args_list[1].args[0]
    assert alias_target.artist_name == "Utada Hikaru"


@pytest.mark.asyncio
async def test_alias_fallback_skipped_when_primary_is_pickable(tmp_path: Path):
    strategy, _indexer, scorer, _store, resolver = _strategy(
        tmp_path, aliases=["Utada Hikaru"]
    )
    scorer.rank = AsyncMock(return_value=[_candidate("manual")])

    ranked = await strategy.search_and_score(_album_task(), timeout=30, auto=0.7, manual=0.5)

    assert [c.tier for c in ranked] == ["manual"]
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_alias_fallback_skipped_without_artist_mbid(tmp_path: Path):
    strategy, _indexer, scorer, _store, resolver = _strategy(
        tmp_path, aliases=["Utada Hikaru"]
    )
    scorer.rank = AsyncMock(return_value=[])

    await strategy.search_and_score(
        _album_task(artist_mbid=None), timeout=30, auto=0.7, manual=0.5
    )

    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_alias_identical_to_primary_name_is_not_researched(tmp_path: Path):
    strategy, indexer, scorer, _store, _resolver = _strategy(
        tmp_path, aliases=["宇多田ヒカル", "  "]
    )
    scorer.rank = AsyncMock(return_value=[])

    ranked = await strategy.search_and_score(_album_task(), timeout=30, auto=0.7, manual=0.5)

    assert ranked == []
    assert indexer.search_album.await_count == 1  # only the primary pass


@pytest.mark.asyncio
async def test_alias_resolver_failure_returns_primary_result(tmp_path: Path):
    strategy, _indexer, scorer, _store, _resolver = _strategy(tmp_path, alias_error=True)
    rejected = [_candidate("rejected", 0.3)]
    scorer.rank = AsyncMock(return_value=rejected)

    ranked = await strategy.search_and_score(_album_task(), timeout=30, auto=0.7, manual=0.5)

    assert ranked == rejected  # degraded, never raises


@pytest.mark.asyncio
async def test_alias_fallback_keeps_primary_result_when_aliases_also_fail(tmp_path: Path):
    strategy, indexer, scorer, _store, _resolver = _strategy(
        tmp_path, aliases=["Utada Hikaru", "Utada"]
    )
    rejected = [_candidate("rejected", 0.3)]
    scorer.rank = AsyncMock(side_effect=[rejected, [], []])

    ranked = await strategy.search_and_score(_album_task(), timeout=30, auto=0.7, manual=0.5)

    assert ranked == rejected
    assert indexer.search_album.await_count == 3  # primary + both aliases


# --- undelivered-file quarantine ---------------------------------------------------


def _write_manifest(tmp_path: Path, task_id="t1", filenames=("peer/01.flac", "peer/02.flac")):
    manifest = DownloadManifest(
        task_id=task_id,
        source_username="peer",
        handle=TaskHandle(source="soulseek", username="peer", filenames=list(filenames)),
        release_group_mbid="rg-1",
        artist_name="a", album_title="b",
        naming_template="{title}.{ext}",
        target_files=[ExpectedFile(filename=f, size=100) for f in filenames],
    )
    (tmp_path / task_id).mkdir(parents=True, exist_ok=True)
    (tmp_path / task_id / "manifest.json").write_bytes(ManifestCodec().encode(manifest))


def _status(succeeded=()):
    return SimpleNamespace(succeeded_filenames=list(succeeded))


@pytest.mark.asyncio
async def test_blocklist_on_failure_quarantines_only_undelivered(tmp_path: Path):
    strategy, _indexer, _scorer, store, _resolver = _strategy(tmp_path)
    _write_manifest(tmp_path)

    await strategy.maybe_blocklist_on_failure(
        _album_task(), _status(succeeded=["peer/01.flac"]),
        completed=False, enumerated_any=True,
    )

    identities = [c.kwargs["identity"] for c in store.record_quarantine.await_args_list]
    assert identities == [soulseek_identity("peer", "peer/02.flac")]
    assert store.record_quarantine.await_args.kwargs["reason"] == "download_failed"
    assert store.record_quarantine.await_args.kwargs["release_group_mbid"] == "rg-1"


@pytest.mark.asyncio
async def test_blocklist_on_abandon_quarantines_all_undelivered(tmp_path: Path):
    strategy, _indexer, _scorer, store, _resolver = _strategy(tmp_path)
    _write_manifest(tmp_path)

    await strategy.maybe_blocklist_on_abandon(_album_task(), _status())

    identities = {c.kwargs["identity"] for c in store.record_quarantine.await_args_list}
    assert identities == {
        soulseek_identity("peer", "peer/01.flac"),
        soulseek_identity("peer", "peer/02.flac"),
    }


@pytest.mark.asyncio
async def test_blocklist_noops_without_manifest_or_username(tmp_path: Path):
    strategy, _indexer, _scorer, store, _resolver = _strategy(tmp_path)

    # no manifest on disk
    await strategy.maybe_blocklist_on_abandon(_album_task(), _status())
    # no source username (nothing was ever enqueued)
    _write_manifest(tmp_path)
    await strategy.maybe_blocklist_on_abandon(
        _album_task(source_username=None), _status()
    )

    store.record_quarantine.assert_not_awaited()
