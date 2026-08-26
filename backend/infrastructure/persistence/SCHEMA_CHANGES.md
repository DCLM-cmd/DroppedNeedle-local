# Schema changes

Append-only ledger of schema changes to the shared SQLite database
(`library.db`). One line per change, appended in the same PR that makes it:

```
date · table · change · reason
```

Scope: any change to a store's `SCHEMA_SQL`/bootstrap DDL or to an additive
ratchet (`_safe_alter`/ALTER TABLE ADD COLUMN) under this package. New tables
count; new columns count; index-only changes that are part of a table's shape
may be noted on the same line.

Rules:

- Append only — never rewrite or reorder earlier lines.
- One line per schema change; group only changes landing as one unit in one PR.
- No retroactive history: changes made before this file was created (2026-08,
  HYGIENE-2) are NOT reconstructed here; `git history` of this directory is
  the archive for everything earlier.
- The migration framework stays out (owner decision, 2026-08): idempotent
  bootstrap DDL + additive ratchets remain the mechanism; this ledger is the
  reviewability record, not a migration runner.
