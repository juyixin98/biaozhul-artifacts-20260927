"""Persistence layer (SQLite).

Split into:

* :mod:`app.storage.db` — connection lifecycle, schema DDL, locking;
* :mod:`app.storage.version_repo` — pattern versions and their patterns;
* :mod:`app.storage.scan_repo` — scan lifecycle state, hit rows, paging;
* :mod:`app.storage.diag_repo` — diagnostic event log.

The database is a single SQLite file in WAL mode. Writes are serialized by an
in-process lock, which is sufficient for a single-process local service and
makes "state persisted then acknowledged" trivially true.
"""
from __future__ import annotations

from .db import Database

__all__ = ["Database"]
