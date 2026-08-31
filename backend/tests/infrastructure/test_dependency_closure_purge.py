"""Cleanup has to follow the schema, not a list of four tables.

Almost every foreign key in this schema is ON DELETE RESTRICT, so a single surviving
reference in any of a hundred and seventy-six tables turns a routine cleanup into a
failure. The purges cleared a hand-written list instead, which is how a live database
ended up with a hundred and forty-eight track rows that no pass could ever collect and
three hundred and thirty-nine album rows for a hundred and sixty-nine albums.
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore

_BIN = "/music/.recycle"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE auth_users (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_users(id) VALUES ('admin')")
    return path


@pytest.fixture
def store(db_path: Path) -> NativeLibraryStore:
    return NativeLibraryStore(db_path, threading.Lock())


def _album(connection, album_id: str = "album-1") -> None:
    connection.execute(
        "INSERT OR IGNORE INTO local_albums(id,root_id,grouping_key,title,title_folded,"
        "album_artist_id,grouping_source,created_at,updated_at) "
        "VALUES (?,'root-1',?,'An Album','an album','artist-1','automatic',1.0,1.0)",
        (album_id, album_id),
    )


def _track(connection, track_id: str, file_path: str, *, album: str = "album-1") -> None:
    _album(connection, album)
    connection.execute(
        "INSERT INTO local_tracks(id,local_album_id,root_id,file_path,relative_path,"
        "path_hash,file_size_bytes,file_mtime_ns,stat_revision,title,title_folded,"
        "album_title,album_title_folded,file_format,ingest_source,imported_at,"
        "membership_source,availability) "
        "VALUES (?,?,'root-1',?,?,?,1,1,'r','A Song','a song','An Album',"
        "'an album','mp3','scan',1.0,'automatic','missing')",
        (track_id, album, file_path, file_path, track_id),
    )


def _plan_item(
    connection,
    *,
    ordinal: int = 0,
    album: str | None = None,
    track: str | None = None,
) -> None:
    """A management plan item, with the job rows it hangs off.

    This is the reference that stranded a hundred and twenty-nine track rows on the
    live database: it is not the track's own data, so a cleanup clearing a fixed list
    of the track's satellite tables never touched it - and it RESTRICTs, so it took
    the whole delete down with it.
    """
    connection.execute(
        "INSERT OR IGNORE INTO library_operation_jobs"
        "(id,kind,state,expected_work_count,created_at,updated_at,row_revision) "
        "VALUES ('job-1','library_management','succeeded',1,1.0,1.0,1)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO library_management_job_snapshots"
        "(job_id,mode,origin,phase,selection_json,profile_revision,"
        "settings_revision,naming_revision,policy_revision,catalog_revision,"
        "profile_snapshot_json,created_at,updated_at) "
        "VALUES ('job-1','apply','manual','applying','{}','p','s','n','po',1,"
        "'{}',1.0,1.0)"
    )
    connection.execute(
        "INSERT INTO library_management_plan_items"
        "(job_id,ordinal,bundle_ordinal,local_album_id,local_track_id,"
        "expected_catalog_revision,expected_policy_revision,expected_profile_revision,"
        "expected_root_id,expected_relative_path,expected_stat_revision,"
        "expected_tag_revision,expected_file_fingerprint,source_path_identity,"
        "desired_document_json,desired_document_hash,eligibility,created_at) "
        "VALUES ('job-1',?,0,?,?,1,'po','p','root-1','a.flac','stat','tag',?,"
        "'identity','{}',?,'eligible',1.0)",
        (ordinal, album, track, "f" * 64, "d" * 64),
    )


@pytest.mark.asyncio
async def test_a_management_plan_no_longer_strands_a_recycled_row(store, db_path):
    """The reference that caused this: a plan item for a file now in the bin. It is
    not the track's own data, so the old four-table list never cleared it, and every
    row it held was pushed down the detach path instead of being removed."""
    with sqlite3.connect(db_path) as connection:
        _track(connection, "t-1", f"{_BIN}/20260101T000000-x/01 - Song.mp3")
        _plan_item(connection, track="t-1")

    assert await store.purge_recycled_track_rows(_BIN) == {"removed": 1, "detached": 0}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_tracks"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM library_management_plan_items"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_row_detached_by_an_earlier_pass_is_collected_later(store, db_path):
    """The stranding bug itself. A row the purge could not delete was written with a
    blank path, then looked for by that same path on the next pass - so it could
    never be seen again. It is found by its marker now, and goes once the reference
    that kept it alive is gone."""
    with sqlite3.connect(db_path) as connection:
        _track(connection, "t-1", f"{_BIN}/20260101T000000-x/01 - Song.mp3")
        connection.execute(
            "INSERT INTO library_play_history"
            "(id,user_id,local_track_id,track_name,artist_name,played_at) "
            "VALUES ('h-1','admin','t-1','A Song','An Artist',1.0)"
        )

    assert await store.purge_recycled_track_rows(_BIN) == {"removed": 0, "detached": 1}

    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM library_play_history WHERE id='h-1'")

    assert await store.purge_recycled_track_rows(_BIN) == {"removed": 1, "detached": 0}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_tracks"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_row_that_is_still_spoken_for_is_not_rewritten_every_pass(
    store, db_path
):
    """Bumping the revision on a row nothing changed would churn the value caches
    key on, once per housekeeping run, forever."""
    with sqlite3.connect(db_path) as connection:
        _track(connection, "t-1", f"{_BIN}/20260101T000000-x/01 - Song.mp3")
        connection.execute(
            "INSERT INTO library_play_history"
            "(id,user_id,local_track_id,track_name,artist_name,played_at) "
            "VALUES ('h-1','admin','t-1','A Song','An Artist',1.0)"
        )

    await store.purge_recycled_track_rows(_BIN)
    with sqlite3.connect(db_path) as connection:
        first = connection.execute(
            "SELECT row_revision FROM local_tracks WHERE id='t-1'"
        ).fetchone()[0]

    assert await store.purge_recycled_track_rows(_BIN) == {"removed": 0, "detached": 0}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT row_revision FROM local_tracks WHERE id='t-1'"
        ).fetchone()[0] == first


@pytest.mark.asyncio
async def test_an_album_is_removed_through_a_second_order_reference(store, db_path):
    """What blocked fifty-one of fifty-one candidates on the live database: a journal
    entry hangs off a plan item, and the plan item is what names the album. Clearing
    only the rows that name the album leaves the journal pointing at one of them."""
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-empty")
        _plan_item(connection, album="album-empty")
        connection.execute(
            "INSERT INTO library_file_mutation_journal"
            "(id,job_id,plan_item_ordinal,subject_kind,subject_key,state,"
            "created_at,updated_at) "
            "VALUES ('j-1','job-1',0,'sidecar','cover.jpg','planned',1.0,1.0)"
        )

    result = await store.purge_empty_album_rows()

    assert result["removed"] == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_albums"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM library_file_mutation_journal"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_an_album_that_still_holds_a_track_is_left_alone(store, db_path):
    """A row owning a missing track records something the library expects to find
    again. The walk would happily delete the track on its way down, so the candidate
    query is the only thing standing between debris and data."""
    with sqlite3.connect(db_path) as connection:
        _track(connection, "t-1", "/music/Artist/Album/01 - Song.mp3")

    assert (await store.purge_empty_album_rows())["removed"] == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_tracks"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_a_sibling_plan_items_journal_is_not_collected_with_it(store, db_path):
    """A composite key has to be matched whole. The journal names its plan item by
    ``(job_id, ordinal)``; following ``job_id`` on its own reaches every entry in the
    job, so removing one album would silently take the file records of every other
    item planned alongside it."""
    with sqlite3.connect(db_path) as connection:
        _album(connection, "album-empty")
        _album(connection, "album-other")
        _plan_item(connection, ordinal=0, album="album-empty")
        _plan_item(connection, ordinal=1, album="album-other")
        for entry, ordinal in (("j-0", 0), ("j-1", 1)):
            connection.execute(
                "INSERT INTO library_file_mutation_journal"
                "(id,job_id,plan_item_ordinal,subject_kind,subject_key,state,"
                "created_at,updated_at) VALUES (?,'job-1',?,'sidecar','cover.jpg',"
                "'planned',1.0,1.0)",
                (entry, ordinal),
            )
        # Keep album-other out of the candidate set the way a real library does.
        _track(connection, "t-1", "/music/Artist/Album/01 - Song.mp3", album="album-other")

    assert (await store.purge_empty_album_rows())["removed"] == 1

    with sqlite3.connect(db_path) as connection:
        surviving = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM library_file_mutation_journal ORDER BY id"
            )
        ]
    assert surviving == ["j-1"]
