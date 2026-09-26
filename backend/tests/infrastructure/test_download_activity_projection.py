import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.download_store import DownloadStore


@pytest.fixture
def store(tmp_path: Path):
    store = DownloadStore(tmp_path / 'downloads.db', threading.Lock())
    with store._connect() as conn:
        conn.execute('CREATE TABLE auth_users(id TEXT PRIMARY KEY)')
        conn.executemany('INSERT INTO auth_users VALUES (?)', [('a',), ('b',)])
    return store


@pytest.mark.asyncio
async def test_landed_corrections_owner_transfer_and_restart(store):
    task = await store.create_task(user_id='a', release_group_mbid='old', artist_name='Artist', album_title='Album')
    await store.update_status(task.id, 'completed', completed_at=10)
    before = await store.get_activity_summary('a', 'user')
    with store._connect() as conn:
        conn.execute("UPDATE download_tasks SET release_group_mbid='new', completed_at=20, user_id='b' WHERE id=?", (task.id,))
    owner = await store.get_activity_summary('a', 'user')
    other = await store.get_activity_summary('b', 'user')
    assert owner.revision > before.revision
    assert owner.landed_release_group_mbids == []
    assert other.landed_release_group_mbids == ['new']
    assert (await store.get_activity_summary('a', 'admin')).landed_release_group_mbids == ['new']
    reopened = DownloadStore(store.db_path, threading.Lock())
    assert (await reopened.get_activity_summary('b', 'user')) == other
    with store._connect() as conn:
        conn.execute('DELETE FROM download_tasks WHERE id=?', (task.id,))
    assert (await reopened.get_activity_summary('b', 'user')).landed_release_group_mbids == []


@pytest.mark.asyncio
async def test_landed_equal_time_boundary_and_effective_time(store):
    with store._connect() as conn:
        conn.executemany(
            "INSERT INTO download_tasks(id,user_id,release_group_mbid,artist_name,album_title,status,created_at,updated_at) VALUES(?,'a',?,'Artist','Album','completed',1,10)",
            [(f't-{i}', f'rg-{i:02}') for i in reversed(range(25))],
        )
    initial = await store.get_activity_summary('a', 'user')
    assert initial.landed_release_group_mbids == [f'rg-{i:02}' for i in range(20)]
    with store._connect() as conn:
        conn.execute("UPDATE download_tasks SET updated_at=30 WHERE id='t-24'")
    corrected = await store.get_activity_summary('a', 'user')
    assert corrected.revision > initial.revision
    assert corrected.landed_release_group_mbids == ['rg-24'] + [f'rg-{i:02}' for i in range(19)]


@pytest.mark.asyncio
async def test_revision_and_projection_share_snapshot_with_concurrent_writer(store):
    task = await store.create_task(user_id='a', release_group_mbid='rg', artist_name='Artist', album_title='Album')
    original_connect = store._connect
    changed = False

    def connect():
        conn = original_connect()
        def trace(sql):
            nonlocal changed
            if not changed and sql.startswith('SELECT COUNT(*) FROM download_tasks'):
                changed = True
                with sqlite3.connect(store.db_path) as writer:
                    writer.execute("UPDATE download_tasks SET status='failed' WHERE id=?", (task.id,))
        conn.set_trace_callback(trace)
        return conn

    store._connect = connect
    before = await store.get_activity_summary('a', 'user')
    after = await store.get_activity_summary('a', 'user')
    assert changed
    assert before.active_count == 1 and before.failed_count == 0
    assert after.active_count == 0 and after.failed_count == 1
    assert after.revision > before.revision
