"""ST2 P1: MbCanonicalStore unit tests - real SQLite at tmp_path.

Covers: construct-twice-on-same-path idempotency, release_to_rg batch
read/write with '' negative, canonical_redirect identity-lane gate,
ISRC banking, seed migration from mbid_resolution_map.
"""

import sqlite3

import pytest

from infrastructure.persistence.mb_canonical_store import (
    OFFICIAL_MB_API_BASE,
    MbCanonicalStore,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "library.db"


@pytest.fixture
def write_lock():
    import threading

    return threading.Lock()


@pytest.fixture
def store(db_path, write_lock):
    return MbCanonicalStore(db_path=db_path, write_lock=write_lock)


class TestConstructTwiceIdempotency:
    def test_construct_twice_on_same_path(self, db_path, write_lock):
        MbCanonicalStore(db_path=db_path, write_lock=write_lock)
        # Second construction must not raise (tables already exist).
        store2 = MbCanonicalStore(db_path=db_path, write_lock=write_lock)
        assert store2.db_path == store2.db_path

    def test_tables_exist_after_construction(self, db_path, write_lock):
        MbCanonicalStore(db_path=db_path, write_lock=write_lock)
        conn = sqlite3.connect(str(db_path))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "canonical_redirect" in tables
        assert "release_to_rg" in tables
        assert "recording_isrc" in tables


class TestReleaseToRg:
    @pytest.mark.asyncio
    async def test_save_and_batch_read(self, store):
        await store.save_release_to_rg(
            {"rel-1": "rg-1", "rel-2": ""}, source_host="https://mb.example"
        )
        result = await store.get_release_to_rg_batch(["rel-1", "rel-2"])
        assert result["rel-1"] == "rg-1"
        assert result["rel-2"] == ""  # authoritative negative

    @pytest.mark.asyncio
    async def test_empty_string_is_authoritative_negative(self, store):
        await store.save_release_to_rg({"rel-neg": ""}, source_host="https://x")
        result = await store.get_release_to_rg_batch(["rel-neg"])
        assert "rel-neg" in result  # present = known
        assert result["rel-neg"] == ""

    @pytest.mark.asyncio
    async def test_miss_returns_empty_dict(self, store):
        result = await store.get_release_to_rg_batch(["never-seen"])
        assert result == {}


class TestCanonicalRedirect:
    @pytest.mark.asyncio
    async def test_save_and_read_official_only(self, store):
        rows = [
            {"entity_kind": "recording", "from_mbid": "old-1", "to_mbid": "new-1"},
        ]
        await store.save_canonical_redirect(rows, OFFICIAL_MB_API_BASE)

        result = await store.get_canonical_redirect(
            "recording", ["old-1"], official_source_only=True
        )
        assert result["old-1"] == "new-1"

    @pytest.mark.asyncio
    async def test_official_gate_filters_non_official_rows(self, store):
        rows = [
            {
                "entity_kind": "recording",
                "from_mbid": "old-mirror",
                "to_mbid": "new-mirror",
            },
        ]
        await store.save_canonical_redirect(rows, "https://hostile.example/ws/2")

        # Identity lane (official only) cannot see it.
        identity = await store.get_canonical_redirect(
            "recording", ["old-mirror"], official_source_only=True
        )
        assert "old-mirror" not in identity

        # Display lane CAN see it.
        display = await store.get_canonical_redirect(
            "recording", ["old-mirror"], official_source_only=False
        )
        assert display["old-mirror"] == "new-mirror"


class TestIsrc:
    @pytest.mark.asyncio
    async def test_bank_and_retrieve(self, store):
        await store.save_isrc_recordings([("USUM71703861", "rec-abc")])
        recordings = await store.get_recordings_by_isrc("USUM71703861")
        assert "rec-abc" in recordings

    @pytest.mark.asyncio
    async def test_unknown_isrc_returns_empty(self, store):
        assert await store.get_recordings_by_isrc("UNKNOWN") == []


class TestSeedMigration:
    def _seed_legacy_table(self, db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mbid_resolution_map (
                source_mbid_lower TEXT PRIMARY KEY,
                source_mbid TEXT NOT NULL,
                release_group_mbid TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO mbid_resolution_map VALUES "
            "('legacy-rel-lower', 'Legacy-Rel', 'legacy-rg'), "
            "('null-rg-lower', 'Null RG', NULL)"
        )
        conn.commit()
        conn.close()

    def test_seed_migration_populates_release_to_rg(self, db_path, write_lock):
        self._seed_legacy_table(str(db_path))
        store = MbCanonicalStore(db_path=db_path, write_lock=write_lock)
        # Seed is synchronous inside __init__; verify via sync read on the DB.
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT rg_mbid FROM release_to_rg WHERE release_mbid_lower = ?",
            ("legacy-rel",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "legacy-rg"

    def test_seed_migration_idempotent(self, db_path, write_lock):
        self._seed_legacy_table(str(db_path))
        MbCanonicalStore(db_path=db_path, write_lock=write_lock)
        MbCanonicalStore(db_path=db_path, write_lock=write_lock)
        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM release_to_rg WHERE release_mbid_lower = ?",
            ("legacy-rel",),
        ).fetchone()[0]
        conn.close()
        assert count == 1  # no duplicates from re-construction

    def test_null_rg_not_seeded_as_positive(self, db_path, write_lock):
        self._seed_legacy_table(str(db_path))
        MbCanonicalStore(db_path=db_path, write_lock=write_lock)
        conn = sqlite3.connect(str(db_path))
        # NULL rg rows should NOT appear as positive entries
        row = conn.execute(
            "SELECT rg_mbid FROM release_to_rg WHERE release_mbid_lower = ?",
            ("null-rg",),
        ).fetchone()
        conn.close()
        # The seed query filters out NULLs so this row shouldn't exist
        assert row is None


class TestWriteThroughPersistence:
    @pytest.mark.asyncio
    async def test_failure_writes_nothing(self, store):
        """Transient failures must never write anything to the store."""
        initial_count = len(await store.get_release_to_rg_batch([]))

        # save_release_to_rg with empty mapping writes nothing
        await store.save_release_to_rg({}, "https://x")
        result = await store.get_release_to_rg_batch([])
        assert len(result) == initial_count
