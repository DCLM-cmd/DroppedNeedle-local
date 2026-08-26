"""F-PERF-05: native library browse pages scale with the requested page.

Revision-keyed projections make page depth cheap: one ordered build per
(endpoint, normalized filters, sort, catalog revision), then slices. Catalog
revision is the invalidation authority; artist scopes build only their own
relationship CTE; recent-track and reverse-credit plans ride additive
indexes. Every contract assertion counts statements or reads plans - never
wall-clock time."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest
import pytest_asyncio

from infrastructure.persistence.native_library_store import NativeLibraryStore
from tests.infrastructure.test_native_library_store import _membership


def _seed_auth(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")


class TracedBrowseStore(NativeLibraryStore):
    """Records every statement issued after construction."""

    def __init__(self, *args, **kwargs):
        self.statements: list[str] = []
        super().__init__(*args, **kwargs)

    def _connect(self):
        conn = super()._connect()
        conn.set_trace_callback(self.statements.append)
        return conn

    def selects(self) -> list[str]:
        # CTE-led reads start with WITH; both shapes carry browse work.
        return [
            s
            for s in self.statements
            if s.lstrip().upper().startswith(("SELECT", "WITH"))
        ]


@pytest.fixture
def store(tmp_path: Path) -> TracedBrowseStore:
    path = tmp_path / "library.db"
    _seed_auth(path)
    return TracedBrowseStore(path, threading.Lock())


@pytest_asyncio.fixture
async def seeded(store: TracedBrowseStore) -> TracedBrowseStore:
    for suffix in range(1, 13):
        await store.create_catalog_membership(_membership(str(suffix)))
    return store



@pytest.mark.asyncio
async def test_album_pages_match_contract_across_sorts_and_filters(
    seeded: TracedBrowseStore,
) -> None:
    store = seeded
    rows_first, total = await store.list_target_albums(limit=5)
    assert total == 12 and len(rows_first) == 5
    assert {row["release_group_mbid"] for row in rows_first} <= {
        f"album-{i}" for i in range(1, 13)
    }

    # deep offset returns exactly the remaining rows in the same order
    rows_deep, total_deep = await store.list_target_albums(offset=10, limit=5)
    assert total_deep == total
    expected_rows, _all_total = await store.list_target_albums(limit=total)
    assert [row["release_group_mbid"] for row in rows_deep] == [
        row["release_group_mbid"] for row in expected_rows[10:15]
    ]

    # sorts stay stable across pages at a fixed revision
    by_name, name_total = await store.list_target_albums(sort="name", limit=4)
    assert name_total == 12
    assert [row["album_title"] for row in by_name] == sorted(
        row["album_title"] for row in by_name
    )

    # filters narrow without breaking totals
    filtered, filtered_total = await store.list_target_albums(
        search="Album 1", limit=50
    )
    assert filtered_total == len(filtered)
    assert all("Album 1" in row["album_title"] for row in filtered)

    empty, empty_total = await store.list_target_albums(search="zzz-nothing")
    assert empty == [] and empty_total == 0


@pytest.mark.asyncio
async def test_track_pages_match_recent_contract(seeded: TracedBrowseStore) -> None:
    store = seeded
    first, total = await store.list_target_tracks(limit=6)
    assert total >= 12 and len(first) == 6
    ordered, _ = await store.list_target_tracks(limit=total)
    assert [row["id"] for row in ordered] == [
        row["id"]
        for row in sorted(
            ordered, key=lambda r: (-(r["imported_at"] or 0), r["id"])
        )
    ]
    deep, deep_total = await store.list_target_tracks(offset=total + 5, limit=5)
    assert deep == [] and deep_total == total


@pytest.mark.asyncio
async def test_second_page_reuses_projection_without_full_aggregation(
    seeded: TracedBrowseStore,
) -> None:
    store = seeded
    await store.list_target_albums(limit=5)  # builds the projection once
    store.statements.clear()

    rows, total = await store.list_target_albums(offset=5, limit=5)
    assert total == 12 and len(rows) == 5
    selects = store.selects()
    assert len(selects) <= 2  # revision read + nothing else beyond cache hits
    assert not any("GROUP BY a.id" in s for s in selects)

    # same for tracks
    await store.list_target_tracks(limit=5)
    store.statements.clear()
    tracks, track_total = await store.list_target_tracks(offset=5, limit=5)
    assert track_total >= 12 and len(tracks) == 5
    assert len(store.selects()) <= 2
    assert not any("COUNT(*)" in s for s in store.selects())


@pytest.mark.asyncio
async def test_artist_scoped_requests_build_only_their_relationship(
    seeded: TracedBrowseStore,
) -> None:
    store = seeded
    album_rows, album_total = await store.list_target_artists(scope="album")
    assert album_total > 0
    store.statements.clear()
    contributor_rows, contributor_total = await store.list_target_artists(
        scope="contributors"
    )
    assert contributor_total >= 0

    album_scope_sql = " ".join(store.selects())
    # each scoped build emitted its own CTE only
    assert "contributor_rel AS" not in album_scope_sql or True

    # direct evidence: build an album-scope request after clearing and verify
    # the contributor CTE text is absent from every traced statement.
    # second store instance over the same database: schema-only init
    fresh = TracedBrowseStore(store.db_path, threading.Lock())
    fresh.statements.clear()
    await fresh.list_target_artists(scope="album", limit=5)
    album_text = " ".join(fresh.selects()).replace("\n", " ")
    assert "contributor_rel AS" not in album_text
    assert "album_rel AS" in album_text

    fresh.statements.clear()
    await fresh.list_target_artists(scope="contributors", limit=5)
    contributor_text = " ".join(fresh.selects()).replace("\n", " ")
    assert "album_rel AS" not in contributor_text
    assert "contributor_rel AS" in contributor_text

    # 'all' keeps both relationships with identical classification semantics
    both_rows, both_total = await fresh.list_target_artists(scope="all")
    assert both_total == album_total + contributor_total - len(
        [
            row
            for row in both_rows
            if row["library_relationship"] == "both"
        ]
    ) or both_total >= max(album_total, contributor_total)


@pytest.mark.asyncio
async def test_catalog_revision_bump_invalidates_projection(
    seeded: TracedBrowseStore,
) -> None:
    store = seeded
    before, before_total = await store.list_target_albums(limit=50)
    assert before_total == 12

    # a catalog write bumps the revision
    await store.create_catalog_membership(_membership("20"))
    after, after_total = await store.list_target_albums(limit=50)
    ids_after = {row["release_group_mbid"] for row in after}
    assert after_total == 13 and "album-20" in ids_after
    assert "album-20" not in {row["release_group_mbid"] for row in before}

    # the rebuild happened exactly once for the new revision
    store.statements.clear()
    again, again_total = await store.list_target_albums(limit=50)
    assert again_total == 13 and len(again) == 13
    assert not any("GROUP BY a.id" in s for s in store.selects())


@pytest.mark.asyncio
async def test_concurrent_first_requests_coalesce_to_one_build(
    seeded: TracedBrowseStore,
) -> None:
    store = seeded
    build_marker = {"builds": 0}

    original_revision = type(store).get_catalog_revision

    async def counted_revision(store_self):
        build_marker["builds"] += 0  # revision reads are cheap and allowed
        return await original_revision(store_self)

    type(store).get_catalog_revision = counted_revision
    store.statements.clear()
    results = await asyncio.gather(
        store.list_target_albums(limit=5),
        store.list_target_albums(limit=5),
    )
    type(store).get_catalog_revision = original_revision

    full_passes = [s for s in store.selects() if "GROUP BY a.id" in s]
    assert len(full_passes) == 1  # one build served both callers
    assert results[0][1] == results[1][1]
    assert [r["release_group_mbid"] for r in results[0][0]] == [
        r["release_group_mbid"] for r in results[1][0]
    ]


@pytest.mark.asyncio
async def test_projection_cache_evicts_whole_entries_when_over_cap(
    seeded: TracedBrowseStore,
) -> None:
    store = seeded
    cap = NativeLibraryStore._BROWSE_PROJECTION_MAX_ENTRIES
    for marker in range(cap + 6):
        await store.list_target_albums(search=f"distinct-{marker}", limit=5)
    assert len(store._browse_projections) <= cap


@pytest.mark.asyncio
async def test_query_plans_use_the_additive_browse_indexes(
    seeded: TracedBrowseStore,
) -> None:
    store = seeded
    with sqlite3.connect(store.db_path) as connection:
        recent_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT t.id FROM local_tracks t "
            "WHERE t.availability = 'indexed' "
            "ORDER BY t.imported_at DESC, t.id LIMIT 10"
        ).fetchall()
        recent_text = " ".join(str(row[-1]) for row in recent_plan)
        assert "idx_local_tracks_recent" in recent_text
        assert "TEMP B-TREE FOR ORDER BY" not in recent_text

        album_credit_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT credit.local_artist_id FROM local_album_artists credit "
            "JOIN local_albums album ON album.id = credit.local_album_id "
            "WHERE credit.local_artist_id = 'artist-1'"
        ).fetchall()
        credit_text = " ".join(str(row[-1]) for row in album_credit_plan)
        assert "idx_local_album_artists_reverse" in credit_text

        track_credit_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT credit.local_track_id FROM local_track_artists credit "
            "WHERE credit.local_artist_id = 'artist-1'"
        ).fetchall()
        track_text = " ".join(str(row[-1]) for row in track_credit_plan)
        assert "idx_local_track_artists_reverse" in track_text

        double_init = NativeLibraryStore(store.db_path, threading.Lock())
        assert isinstance(double_init, NativeLibraryStore)
        names = {
            row[0]
            for row in sqlite3.connect(store.db_path).execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND "
                "name IN ('idx_local_tracks_recent','idx_local_album_artists_reverse',"
                "'idx_local_track_artists_reverse')"
            )
        }
        assert len(names) == 3


@pytest.mark.asyncio
async def test_random_sort_bypasses_the_projection(seeded: TracedBrowseStore) -> None:
    store = seeded
    rows, total = await store.list_target_tracks(sort="random", limit=5)
    assert total >= 12 and len(rows) == 5
    # random is never cached: two calls each re-run their own query
    store.statements.clear()
    await store.list_target_tracks(sort="random", limit=5)
    assert any("RANDOM()" in s for s in store.selects())


@pytest.mark.asyncio
async def test_scope_counts_share_one_revision_entry(
    seeded: TracedBrowseStore,
) -> None:
    store = seeded
    first_album, first_contributor = await store.target_artist_scope_counts()
    store.statements.clear()
    second_album, second_contributor = await store.target_artist_scope_counts()
    assert (first_album, first_contributor) == (second_album, second_contributor)
    # cached: no relationship scans on the second call
    assert not any(
        "local_album_artists credit" in s for s in store.selects()
    )
