"""Stat-derived change detection for hashless scans.

Guarantee boundary (F-029, accepted tradeoff): a revision is
``size:mtime_ns`` ONLY. A content swap that preserves size and lands in the
same mtime tick classifies as ``unchanged``, and on NFS a client attribute
cache (actimeo up to ~60s) can serve pre-write mtimes, so a scan immediately
after an out-of-band edit may also report ``unchanged`` until attributes
expire AND another scan runs. Folding ``st_ctime_ns`` into the revision would
narrow this but is a one-time mass-``changed`` promotion against the legacy
``stat_revision_kind`` machinery - it requires explicit owner sign-off before
anyone reaches for it.
"""

from os import stat_result


def exact_stat_revision(file_size_bytes: int, file_mtime_ns: int) -> str:
    return f"{file_size_bytes}:{file_mtime_ns}"


def revision_from_stat(stat: stat_result) -> str:
    return exact_stat_revision(stat.st_size, stat.st_mtime_ns)
