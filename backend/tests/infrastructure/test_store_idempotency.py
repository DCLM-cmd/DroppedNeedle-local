"""T-STORE: every SQLite store must be constructible twice on the same file.

Second construction exercises ``CREATE TABLE IF NOT EXISTS`` + additive
``_safe_alter`` ratchets as no-ops. Non-SQLite modules (e.g. the JSON-file
``YouTubeQuotaStore``) and stores needing extra constructor args are excluded
by discovery, not by hand-list.
"""

import importlib
import inspect
import sqlite3
import threading
from pathlib import Path

import pytest

from infrastructure.persistence import _database


def _discover_store_classes():
    pkg = Path(_database.__file__).parent
    found = []
    for f in sorted(pkg.glob("*_store.py")):
        mod = importlib.import_module(f"infrastructure.persistence.{f.stem}")
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if (
                not issubclass(cls, _database.PersistenceBase)
                or cls is _database.PersistenceBase
                or cls.__module__ != mod.__name__
            ):
                continue
            params = [
                p
                for p in inspect.signature(cls.__init__).parameters.values()
                if p.name != "self"
            ]
            extra_required = any(
                p.default is inspect.Parameter.empty
                and p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
                for p in params[2:]
            )
            if not extra_required:
                found.append(pytest.param(cls, id=cls.__name__))
    return found


@pytest.mark.parametrize("store_cls", _discover_store_classes())
def test_store_double_construct_is_idempotent(store_cls, tmp_path):
    db = tmp_path / "shared.db"
    lock = threading.Lock()
    first = store_cls(db, lock)
    assert db.exists()
    second = store_cls(db, lock)
    assert second.db_path == first.db_path
    tables = [
        row[0]
        for row in sqlite3.connect(db).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]
    assert tables, f"{store_cls.__name__} created no tables"
