"""Version repository: compiled pattern-set metadata + pattern rows."""
from __future__ import annotations

from typing import List, Optional

from ..storage.db import utc_now


class VersionRepo:
    def __init__(self, db):
        self._db = db

    def insert_version(
        self,
        version_id: str,
        name: str,
        encoding: str,
        case_mode: str,
        pattern_b64: List[str],
        node_count: int,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO versions(version_id, name, encoding, case_mode, "
                "pattern_count, node_count, created_at) VALUES (?,?,?,?,?,?,?)",
                (version_id, name, encoding, case_mode, len(pattern_b64),
                 node_count, utc_now()),
            )
            conn.executemany(
                "INSERT INTO patterns(version_id, pattern_id, pattern_b64, "
                "length) VALUES (?,?,?,?)",
                [(version_id, pid, b64, _b64_len(b64))
                 for pid, b64 in enumerate(pattern_b64)],
            )

    def get(self, version_id: str):
        return self._db.execute(
            "SELECT * FROM versions WHERE version_id=?", (version_id,)
        ).fetchone()

    def exists(self, version_id: str) -> bool:
        return self.get(version_id) is not None

    def list_versions(self, limit: int = 100):
        return self._db.execute(
            "SELECT * FROM versions ORDER BY created_at DESC, version_id "
            "LIMIT ?",
            (limit,),
        ).fetchall()

    def get_patterns(self, version_id: str) -> List[str]:
        rows = self._db.execute(
            "SELECT pattern_b64 FROM patterns WHERE version_id=? "
            "ORDER BY pattern_id",
            (version_id,),
        ).fetchall()
        return [r["pattern_b64"] for r in rows]


def _b64_len(b64: str) -> int:
    import base64
    return len(base64.b64decode(b64, validate=True))
