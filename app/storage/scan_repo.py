"""Scan repository: scan lifecycle rows, hit persistence and keyset paging."""
from __future__ import annotations

from typing import List, Optional

from .db import utc_now


class ScanRepo:
    def __init__(self, db):
        self._db = db

    # ---- lifecycle ----------------------------------------------------------

    def insert_scan(self, scan_id: str, version_id: str) -> None:
        now = utc_now()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO scans(scan_id, version_id, state, current_node, "
                "bytes_consumed, epoch, created_at, updated_at) "
                "VALUES (?,?, 'open', 0, 0, 1, ?, ?)",
                (scan_id, version_id, now, now),
            )

    def get(self, scan_id: str):
        return self._db.execute(
            "SELECT * FROM scans WHERE scan_id=?", (scan_id,)
        ).fetchone()

    def update_runtime(self, scan_id: str, node: int, bytes_consumed: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE scans SET current_node=?, bytes_consumed=?, "
                "updated_at=? WHERE scan_id=?",
                (node, bytes_consumed, utc_now(), scan_id),
            )

    def reset(self, scan_id: str, version_id: str) -> int:
        """Explicit reset/version switch at the boundary. Returns new epoch."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT epoch FROM scans WHERE scan_id=?", (scan_id,)
            ).fetchone()
            new_epoch = row["epoch"] + 1
            conn.execute(
                "UPDATE scans SET version_id=?, current_node=0, "
                "bytes_consumed=0, epoch=?, updated_at=? WHERE scan_id=?",
                (version_id, new_epoch, utc_now(), scan_id),
            )
            # Old hits are retained (audit) but queried per-epoch only, so a
            # stale cursor can never return rows from the previous automaton.
            return new_epoch

    def close(self, scan_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE scans SET state='closed', updated_at=? WHERE scan_id=?",
                (utc_now(), scan_id),
            )

    # ---- hits ---------------------------------------------------------------

    def next_seq(self, scan_id: str) -> int:
        """First free seq within this scan across ALL epochs (0-based).

        seq is globally unique within a scan (UNIQUE(scan_id, seq)); pages
        filter by epoch, so resetting simply starts a new contiguous range
        rather than reusing numbers — which also makes stale cursors safe.
        """
        row = self._db.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM hits "
            "WHERE scan_id=?",
            (scan_id,),
        ).fetchone()
        return row["m"] + 1

    def insert_hits(
        self,
        scan_id: str,
        epoch: int,
        first_seq: int,
        hits: List[tuple],
    ) -> None:
        """hits: iterable of (start, end, pattern_id), already canonical."""
        with self._db.transaction() as conn:
            conn.executemany(
                "INSERT INTO hits(scan_id, epoch, seq, start_off, end_off, "
                "pat_id) VALUES (?,?,?,?,?,?)",
                [
                    (scan_id, epoch, first_seq + i, s, e, pid)
                    for i, (s, e, pid) in enumerate(hits)
                ],
            )

    def total_hits(self, scan_id: str, epoch: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS c FROM hits WHERE scan_id=? AND epoch=?",
            (scan_id, epoch),
        ).fetchone()
        return row["c"]

    def page_hits(
        self, scan_id: str, epoch: int, after_seq: int, limit: int
    ) -> List:
        """Keyset page over the stable seq order (seq > after_seq)."""
        return self._db.execute(
            "SELECT seq, start_off, end_off, pat_id FROM hits "
            "WHERE scan_id=? AND epoch=? AND seq>? "
            "ORDER BY seq ASC LIMIT ?",
            (scan_id, epoch, after_seq, limit),
        ).fetchall()
