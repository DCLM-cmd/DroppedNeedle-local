"""Labeled connection-local SQLite settings telemetry.

The running application's real connections are the only source of truth for
``synchronous``, ``busy_timeout``, and ``wal_autocheckpoint`` (a fresh read-only
probe connection reports only its own defaults). Each store role reports its
connection-local values once per process, labeled by role, so an operator can
compare the actual runtime settings against the owner-recorded policy without
in-process metric plumbing.

Bounded by design: one line per role per process, no per-connection identity.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_PRAGMAS = ("journal_mode", "synchronous", "busy_timeout", "wal_autocheckpoint")

_reported: set[str] = set()
_lock = threading.Lock()


def report_connection_settings(role: str, connection: Any) -> None:
    """Emit one labeled INFO line per role per process with connection-local PRAGMAs."""
    with _lock:
        if role in _reported:
            return
        _reported.add(role)
    try:
        values = {
            name: connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in _PRAGMAS
        }
    except Exception:  # noqa: BLE001 - telemetry must never break a connection
        logger.warning("sqlite connection_settings role=%s unavailable", role)
        return
    rendered = " ".join(f"{name}={values[name]}" for name in _PRAGMAS)
    logger.info("sqlite connection_settings role=%s %s", role, rendered)
