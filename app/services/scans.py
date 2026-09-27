"""Scan/session service: streaming state, version switching and hit paging.

State model
-----------
* Durable truth lives in SQLite (``scans`` row + ``hits`` rows + epoch).
* A process-local ``StreamingMatcher`` cache holds the hot streaming state.
  On a cache miss the matcher is rebuilt from the scan's pinned version and
  its ``(node, bytes_consumed, epoch)`` is restored from the durable row —
  node ids are only ever interpreted with that exact automaton.

Version switching
-----------------
``reset_scan`` is the *only* path that changes a scan's pattern set. It is an
explicit client action at a stream boundary; it replaces the matcher's bound
automaton, rewinds to root and bumps the epoch. Hits and cursors from the old
epoch are never mixed with the new automaton's output.

Pagination
----------
Hits carry a per-epoch monotonic ``seq`` assigned in canonical emission order.
Pages are keyset ranges ``(seq > after)``, and the opaque cursor HMACs
``(scan_id, epoch, after_seq)``. A single chunk producing tens of thousands of
hits therefore drains over repeated resumable calls; nothing is buffered in
HTTP memory.
"""
from __future__ import annotations

import threading
import uuid
from typing import Dict, List, Optional, Tuple

from ..errors import (
    InvalidCursorError,
    LimitOutOfRangeError,
    ScanClosedError,
    ScanNotFoundError,
    StaleCursorError,
    VersionMismatchError,
)
from ..matcher import StreamingMatcher
from ..pagination import PageCursor
from ..spec import decode_base64, encode_base64
from ..storage.scan_repo import ScanRepo
from .versions import VersionService


class ScanService:
    def __init__(self, repo: ScanRepo, versions: VersionService, *,
                 secret: str, default_page_limit: int, max_page_limit: int,
                 max_chunk_bytes: int):
        self._repo = repo
        self._versions = versions
        self._secret = secret
        self._default_limit = default_page_limit
        self._max_limit = max_page_limit
        self._max_chunk = max_chunk_bytes
        self._matchers: Dict[str, StreamingMatcher] = {}
        self._guard = threading.RLock()

    # ---- internal helpers ---------------------------------------------------

    def _require_row(self, scan_id: str):
        row = self._repo.get(scan_id)
        if row is None:
            raise ScanNotFoundError(
                f"scan {scan_id} does not exist",
                details={"scan_id": scan_id},
            )
        return row

    def _get_matcher(self, scan_id: str) -> StreamingMatcher:
        """Return hot matcher, rebuilding+restoring it on a cache miss."""
        m = self._matchers.get(scan_id)
        if m is not None:
            return m
        row = self._require_row(scan_id)
        automaton, spec = self._versions.load(row["version_id"])
        m = StreamingMatcher(row["version_id"], automaton, spec)
        m.restore(
            node=row["current_node"],
            bytes_consumed=row["bytes_consumed"],
            epoch=row["epoch"],
        )
        self._matchers[scan_id] = m
        return m

    # ---- lifecycle ----------------------------------------------------------

    def open_scan(self, version_id: str) -> Tuple[str, StreamingMatcher]:
        # Raises VersionNotFoundError before creating anything.
        automaton, _spec = self._versions.load(version_id)
        scan_id = uuid.uuid4().hex
        self._repo.insert_scan(scan_id, version_id)
        matcher = StreamingMatcher(version_id, automaton, _spec)
        with self._guard:
            self._matchers[scan_id] = matcher
        return scan_id, matcher

    def status(self, scan_id: str) -> dict:
        row = self._require_row(scan_id)
        matcher = self._get_matcher(scan_id)
        st = matcher.status()
        assert st.epoch == row["epoch"], "hot/durable epoch drift"
        return {
            "scan_id": scan_id,
            "version_id": row["version_id"],
            "state": row["state"],
            "state_node": st.state_node,
            "bytes_consumed": st.bytes_consumed,
            "epoch": st.epoch,
            "pattern_count": st.pattern_count,
            "node_count": st.node_count,
            "total_hits_in_epoch": self._repo.total_hits(scan_id, st.epoch),
        }

    def close_scan(self, scan_id: str) -> dict:
        row = self._require_row(scan_id)
        if row["state"] != "open":
            raise ScanClosedError(
                f"scan {scan_id} is already closed",
                details={"scan_id": scan_id},
            )
        self._repo.close(scan_id)
        with self._guard:
            self._matchers.pop(scan_id, None)
        return self.status(scan_id)

    # ---- streaming ----------------------------------------------------------

    def feed_chunk(
        self,
        scan_id: str,
        *,
        chunk_b64: str,
        declared_version_id: Optional[str],
    ) -> dict:
        row = self._require_row(scan_id)
        if row["state"] != "open":
            raise ScanClosedError(
                f"scan {scan_id} is closed; open a new scan to continue",
                details={"scan_id": scan_id},
            )
        # Version guard: a chunk may not silently target another version.
        if (declared_version_id is not None
                and declared_version_id != row["version_id"]):
            raise VersionMismatchError(
                f"scan is pinned to version {row['version_id']} but chunk "
                f"declares {declared_version_id}; reset the scan at an "
                f"explicit boundary to switch versions",
                details={"scan_version": row["version_id"],
                         "chunk_version": declared_version_id},
            )

        raw = decode_base64(chunk_b64, what="chunk")
        if len(raw) > self._max_chunk:
            from ..errors import DomainError
            raise DomainError(
                f"chunk is {len(raw)} bytes, limit {self._max_chunk}",
                code="chunk_too_large", http_status=422,
                decision="reject",
                details={"length": len(raw), "limit": self._max_chunk},
            )

        with self._guard:
            matcher = self._get_matcher(scan_id)
            # Re-read inside the lock: reset cannot interleave below us.
            row = self._repo.get(scan_id)
            if matcher.version_id != row["version_id"]:
                # Defensive; reset() also replaces the bound automaton.
                raise VersionMismatchError(
                    "hot matcher and durable row disagree on version",
                    details={"matcher_version": matcher.version_id,
                             "row_version": row["version_id"]},
                )
            epoch_before = matcher.epoch
            hits = matcher.push(raw)  # typed errors raised pre-mutation
            st = matcher.status()
            first_seq = self._repo.next_seq(scan_id)
            self._repo.insert_hits(scan_id, st.epoch, first_seq, hits)
            self._repo.update_runtime(
                scan_id, st.state_node, st.bytes_consumed
            )
            assert epoch_before == st.epoch, "epoch changed during feed"

        return {
            "scan_id": scan_id,
            "version_id": row["version_id"],
            "bytes_consumed": st.bytes_consumed,
            "state_node": st.state_node,
            "epoch": st.epoch,
            "new_hits": len(hits),
            "first_seq": first_seq if hits else None,
            "total_hits_in_epoch": self._repo.total_hits(
                scan_id, st.epoch),
        }

    # ---- explicit boundary: version switch / rewind -------------------------

    def reset_scan(
        self, scan_id: str, new_version_id: str
    ) -> dict:
        row = self._require_row(scan_id)
        if row["state"] != "open":
            raise ScanClosedError(
                f"cannot reset closed scan {scan_id}",
                details={"scan_id": scan_id},
            )
        if new_version_id == row["version_id"]:
            # Rewind same version is allowed as an explicit boundary, but the
            # automaton must be the same immutable instance semantics.
            automaton, spec = self._versions.load(new_version_id)
        else:
            # Validates existence (VersionNotFoundError) before mutating.
            automaton, spec = self._versions.load(new_version_id)

        with self._guard:
            new_epoch = self._repo.reset(scan_id, new_version_id)
            matcher = self._get_matcher(scan_id)
            matcher.reset(new_version_id, automaton, spec)
            assert matcher.epoch == new_epoch
        return self.status(scan_id)

    # ---- introspection for diagnostics (content-free labels) ---------------

    def pattern_lengths(self, scan_id: str, pattern_ids: List[int]) -> List[int]:
        matcher = self._get_matcher(scan_id)
        return [matcher.automaton.pattern_length(pid) for pid in pattern_ids]

    # ---- resumable paging ---------------------------------------------------

    def page_hits(
        self,
        scan_id: str,
        *,
        cursor_token: Optional[str],
        limit: Optional[int],
    ) -> dict:
        row = self._require_row(scan_id)
        page_limit = limit if limit is not None else self._default_limit
        if page_limit < 1 or page_limit > self._max_limit:
            raise LimitOutOfRangeError(
                f"limit must be in [1, {self._max_limit}]",
                details={"limit": page_limit,
                         "max": self._max_limit},
            )

        if cursor_token is None:
            after_seq = -1
            cursor_epoch = row["epoch"]
        else:
            cur = PageCursor.from_token(cursor_token, self._secret)
            if cur.scan_id != scan_id:
                raise InvalidCursorError(
                    "cursor belongs to a different scan",
                    details={"cursor_scan": cur.scan_id},
                )
            if cur.epoch != row["epoch"]:
                raise StaleCursorError(
                    f"cursor epoch {cur.epoch} is stale; scan epoch is "
                    f"{row['epoch']} after a reset/version switch. Restart "
                    f"paging without a cursor.",
                    details={"cursor_epoch": cur.epoch,
                             "current_epoch": row["epoch"]},
                )
            after_seq = cur.after_seq
            cursor_epoch = cur.epoch

        rows = self._repo.page_hits(
            scan_id, cursor_epoch, after_seq, page_limit
        )
        matcher = self._get_matcher(scan_id)
        items = []
        last_seq = after_seq
        for r in rows:
            pid = r["pat_id"]
            length = matcher.automaton.pattern_length(pid)
            items.append({
                "seq": r["seq"],
                "start": r["start_off"],
                "end": r["end_off"],
                "pattern_id": pid,
                "pattern_length": length,
                "pattern_b64": encode_base64(matcher.pattern_label(pid)),
            })
            last_seq = r["seq"]

        total = self._repo.total_hits(scan_id, cursor_epoch)
        last_seq = after_seq
        for r in rows:
            last_seq = r["seq"]
        # More rows remain iff the greatest seq served is not the final seq.
        has_more = bool(items) and (total > last_seq + 1)
        next_cursor = (
            PageCursor(scan_id=scan_id, epoch=cursor_epoch,
                       after_seq=last_seq).to_token(self._secret)
            if has_more else None
        )
        return {
            "scan_id": scan_id,
            "version_id": row["version_id"],
            "epoch": cursor_epoch,
            "limit": page_limit,
            "total_in_epoch": total,
            "returned": len(items),
            "next_cursor": next_cursor if has_more else None,
            "hits": items,
        }
