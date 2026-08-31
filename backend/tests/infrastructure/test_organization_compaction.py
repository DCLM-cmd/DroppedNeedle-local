"""Finished organization runs give back the artwork bytes they are holding.

Inline base64 covers were 2.67 GB of a 2.90 GB database: one cover is stored in a plan
item's desired document AND in its catalog document, and again in every file of the
import bundle, so a 14-track album kept roughly thirty copies of the same image.

The bytes are read only while a run is executing. These tests pin both halves of that
claim - that compaction removes them, and that everything organization still needs
survives it.
"""

import base64
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence.native_library_store import NativeLibraryStore

_COVER = base64.b64encode(b"pretend-this-is-a-six-megabyte-cover").decode()


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


def _document(*, artwork: int = 1) -> str:
    return json.dumps(
        {
            "fields": [{"name": "title", "value": "A Song"}],
            "artwork": [
                {
                    "image_type": "front",
                    "mime_type": "image/jpeg",
                    "description": "cover",
                    "width": 500,
                    "height": 500,
                    "byte_size": 36,
                    "sha256": f"{index:064d}",
                    "content": _COVER,
                    "format_supported": True,
                }
                for index in range(artwork)
            ],
        }
    )


def _seed_plan_item(db_path: Path, *, job_state: str, artwork: int = 1) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO library_operation_jobs"
            "(id,kind,state,expected_work_count,created_at,updated_at,row_revision) "
            "VALUES ('job-1','library_management',?,1,1.0,1.0,1)",
            (job_state,),
        )
        connection.execute(
            "INSERT INTO library_management_job_snapshots"
            "(job_id,mode,origin,phase,selection_json,profile_revision,"
            "settings_revision,naming_revision,policy_revision,catalog_revision,"
            "profile_snapshot_json,created_at,updated_at) "
            "VALUES ('job-1','apply','manual','applying','{}','p','s','n','po',1,"
            "'{}',1.0,1.0)"
        )
        document = _document(artwork=artwork)
        connection.execute(
            "INSERT INTO library_management_plan_items"
            "(job_id,ordinal,bundle_ordinal,local_album_id,expected_catalog_revision,"
            "expected_policy_revision,expected_profile_revision,expected_root_id,"
            "expected_relative_path,expected_stat_revision,expected_tag_revision,"
            "expected_file_fingerprint,source_path_identity,desired_document_json,"
            "desired_document_hash,catalog_document_json,eligibility,created_at) "
            "VALUES ('job-1',0,0,'album-1',1,'po','p','root-1','a.flac','stat','tag',"
            "?,'identity',?,?,?,'eligible',1.0)",
            ("f" * 64, document, "d" * 64, document),
        )


def _plan_documents(db_path: Path) -> tuple[dict, dict]:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT desired_document_json, catalog_document_json "
            "FROM library_management_plan_items"
        ).fetchone()
    return json.loads(row[0]), json.loads(row[1])


# ---- plan items ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_finished_runs_artwork_bytes_are_dropped(store, db_path) -> None:
    _seed_plan_item(db_path, job_state="succeeded")

    counts = await store.compact_finished_organization_records()

    desired, catalog = _plan_documents(db_path)
    assert "content" not in desired["artwork"][0]
    assert "content" not in catalog["artwork"][0]
    assert counts["plan_documents"] == 2


@pytest.mark.asyncio
async def test_everything_identifying_the_image_survives(store, db_path) -> None:
    """The audit trail must still say which artwork a run applied."""
    _seed_plan_item(db_path, job_state="succeeded")

    await store.compact_finished_organization_records()

    image = _plan_documents(db_path)[0]["artwork"][0]
    assert image["sha256"] == f"{0:064d}"
    assert (image["width"], image["height"], image["byte_size"]) == (500, 500, 36)
    assert image["mime_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_the_rest_of_the_document_is_untouched(store, db_path) -> None:
    _seed_plan_item(db_path, job_state="succeeded")

    await store.compact_finished_organization_records()

    assert _plan_documents(db_path)[0]["fields"] == [
        {"name": "title", "value": "A Song"}
    ]


@pytest.mark.asyncio
async def test_documents_with_several_images_are_fully_compacted(store, db_path):
    """Removing an element shifts the rest down, so index 0 is stripped repeatedly."""
    _seed_plan_item(db_path, job_state="succeeded", artwork=3)

    await store.compact_finished_organization_records()

    assert all("content" not in i for i in _plan_documents(db_path)[0]["artwork"])


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "running", "paused", "ready"])
async def test_a_run_that_can_still_execute_keeps_its_artwork(store, db_path, state):
    """The tag writer reads these bytes. Taking them from a live run would make it
    stamp empty artwork into every file it touches."""
    _seed_plan_item(db_path, job_state=state)

    counts = await store.compact_finished_organization_records()

    assert counts["plan_documents"] == 0
    assert _plan_documents(db_path)[0]["artwork"][0]["content"] == _COVER


@pytest.mark.asyncio
async def test_compacting_twice_changes_nothing_further(store, db_path) -> None:
    _seed_plan_item(db_path, job_state="succeeded")
    await store.compact_finished_organization_records()

    counts = await store.compact_finished_organization_records()

    assert counts["plan_documents"] == 0


# ---- import bundles ---------------------------------------------------------------

def _seed_bundle(db_path: Path, *, state: str) -> None:
    request = {
        "idempotency_key": "key-1",
        "origin": "acquisition",
        "policy_revision": "p",
        "conversion_job_id": "conversion-7",
        "files": [
            {
                "download_task_id": "task-9",
                "source_path": "/music/a.flac",
                "desired_document": json.loads(_document()),
                "artifacts": [
                    {
                        "kind": "external_art",
                        "destination_relative_path": "cover.jpg",
                        "content": _COVER,
                        "source_fingerprint": "f" * 64,
                    }
                ],
            }
        ],
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO library_management_import_bundles"
            "(id,idempotency_key,origin,policy_revision,request_json,request_hash,"
            "state,created_at,updated_at,row_revision) "
            "VALUES ('bundle-1','key-1','acquisition','p',?,?,?,1.0,1.0,1)",
            (json.dumps(request), "a" * 64, state),
        )


def _bundle_request(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT request_json FROM library_management_import_bundles"
        ).fetchone()
    return json.loads(row[0])


@pytest.mark.asyncio
async def test_a_completed_bundle_gives_up_its_images(store, db_path) -> None:
    _seed_bundle(db_path, state="completed")

    counts = await store.compact_finished_organization_records()

    request = _bundle_request(db_path)
    file_entry = request["files"][0]
    assert "content" not in file_entry["desired_document"]["artwork"][0]
    assert "content" not in file_entry["artifacts"][0]
    assert counts["bundles"] == 1


@pytest.mark.asyncio
async def test_the_fields_the_bundle_queries_read_are_preserved(store, db_path) -> None:
    """Two queries reach into this JSON: one looks up ``$.conversion_job_id``, the
    other joins on ``$.files[*].download_task_id``. Both must still resolve."""
    _seed_bundle(db_path, state="completed")

    await store.compact_finished_organization_records()

    with sqlite3.connect(db_path) as connection:
        by_conversion = connection.execute(
            "SELECT id FROM library_management_import_bundles WHERE "
            "json_extract(request_json,'$.conversion_job_id')='conversion-7'"
        ).fetchone()
        by_task = connection.execute(
            "SELECT DISTINCT bundle.id FROM library_management_import_bundles bundle, "
            "json_each(bundle.request_json,'$.files') file "
            "WHERE json_extract(file.value,'$.download_task_id')='task-9'"
        ).fetchone()

    assert by_conversion[0] == "bundle-1"
    assert by_task[0] == "bundle-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["publishing", "catalog_committed", "cleanup_pending"])
async def test_a_bundle_that_is_not_finished_keeps_its_images(store, db_path, state):
    """A publishing bundle's sealed request is still decoded and acted on."""
    _seed_bundle(db_path, state=state)

    counts = await store.compact_finished_organization_records()

    assert counts["bundles"] == 0
    assert _bundle_request(db_path)["files"][0]["artifacts"][0]["content"] == _COVER


@pytest.mark.asyncio
async def test_a_compacted_bundle_is_not_visited_again(store, db_path) -> None:
    _seed_bundle(db_path, state="completed")
    await store.compact_finished_organization_records()

    counts = await store.compact_finished_organization_records()

    assert counts["bundles"] == 0
    assert _bundle_request(db_path)["_artwork_compacted"] is True


@pytest.mark.asyncio
async def test_unreadable_bundle_json_is_skipped_not_fatal(store, db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO library_management_import_bundles"
            "(id,idempotency_key,origin,policy_revision,request_json,request_hash,"
            "state,created_at,updated_at,row_revision) "
            "VALUES ('bundle-2','key-2','acquisition','p','{not json',?,'completed',"
            "1.0,1.0,1)",
            ("b" * 64,),
        )

    counts = await store.compact_finished_organization_records()

    assert counts["bundles"] == 0
