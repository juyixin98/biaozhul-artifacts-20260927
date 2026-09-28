"""Offline journal replay.

Reads an exported journal (a JSON file produced by the state service, or a
file of such records), verifies every record's HMAC, replays each change on
a *fresh* in-memory kernel + target store, and checks that the recomputed
root matches the signed ``new_root`` and that records chain via
``prev_root``.

This is intentionally independent of the service write path: it does not
trust the journal author beyond the HMAC and it rebuilds roots itself.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional

from ..crypto.encoding import normalize_key, normalize_value
from ..crypto.hashing import empty_at, verify_payload
from ..kernel.store import NodeStore
from ..kernel.tree import SparseMerkleTree


class ReplayError(Exception):
    def __init__(self, category: str, message: str, seq: Optional[int] = None, **state) -> None:
        super().__init__(message)
        self.category = category
        self.seq = seq
        self.state = state


@dataclass
class ReplayOutcome:
    applied: int
    skipped_noop: int
    final_root: bytes
    expected_final_root: bytes
    first_seq: Optional[int]
    last_seq: Optional[int]

    @property
    def root_matches(self) -> bool:
        return self.final_root == self.expected_final_root


def load_journal_file(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "records" in data:
        data = data["records"]
    if not isinstance(data, list):
        raise ReplayError("bad_file", "journal must be a JSON list or {'records': [...]}")
    return data


def replay_records(
    records: List[dict],
    target_store: NodeStore,
    hmac_key: str,
    *,
    start_root: Optional[bytes] = None,
) -> ReplayOutcome:
    """Verify and replay records.  Raises ReplayError on the first failure."""
    tree = SparseMerkleTree(target_store, start_root if start_root is not None else empty_at(0))
    applied = 0
    skipped = 0
    first_seq: Optional[int] = None
    last_signed_root = tree.root
    prev_seq: Optional[int] = None

    for i, row in enumerate(records):
        seq = row.get("seq")
        if not isinstance(seq, int) or seq < 0:
            raise ReplayError("bad_record", "record missing integer seq", seq=seq, index=i)
        if first_seq is None:
            first_seq = seq
        if prev_seq is not None and seq != prev_seq + 1:
            raise ReplayError(
                "sequence_gap",
                f"journal not contiguous: seq {prev_seq} followed by {seq}",
                seq=seq,
            )
        prev_seq = seq

        payload = row.get("payload")
        if payload is None:
            # Exported rows keep canonical bytes in payload_json; reconstruct.
            pj = row.get("payload_json")
            if pj is None:
                raise ReplayError("bad_record", "record has neither payload nor payload_json", seq=seq)
            try:
                payload = json.loads(pj)
            except (ValueError, TypeError) as exc:
                raise ReplayError("bad_record", "payload_json is not valid JSON", seq=seq) from exc
        signature = row.get("signature_hex", "")
        if not verify_payload(payload, signature, hmac_key):
            raise ReplayError(
                "bad_signature",
                "HMAC verification failed: record was tampered with or key differs",
                seq=seq,
                key_hex=_short(payload.get("key_hex")),
            )

        for field in ("version", "kind", "key_hex", "prev_root", "new_root"):
            if field not in payload:
                raise ReplayError("bad_record", f"payload missing {field}", seq=seq)
        if payload["version"] != "smt-v1":
            raise ReplayError("bad_record", f"unsupported version {payload['version']!r}", seq=seq)
        if payload["kind"] not in ("set", "delete"):
            raise ReplayError("bad_record", f"unknown kind {payload['kind']!r}", seq=seq)

        try:
            key = normalize_key(payload["key_hex"])
            value = (
                None
                if payload["kind"] == "delete"
                else normalize_value(bytes.fromhex(payload["value_hex"]))
            )
            expected_prev = bytes.fromhex(payload["prev_root"])
            expected_new = bytes.fromhex(payload["new_root"])
        except (ValueError, TypeError) as exc:
            raise ReplayError("bad_record", f"unparseable field: {exc}", seq=seq) from exc

        if expected_prev != tree.root:
            raise ReplayError(
                "chain_break",
                "prev_root does not match the replayer's current root "
                "(missing, reordered or forked record)",
                seq=seq,
                expected_prev=expected_prev.hex()[:16],
                actual_root=tree.root.hex()[:16],
            )

        before = tree.root
        tree.update(key, value)
        if tree.root == before:
            skipped += 1  # signed no-ops never appear in a healthy journal
        else:
            applied += 1
        if tree.root != expected_new:
            raise ReplayError(
                "root_mismatch",
                "replaying the signed change produced a different new_root",
                seq=seq,
                expected=expected_new.hex()[:16],
                actual=tree.root.hex()[:16],
            )
        last_signed_root = expected_new

    return ReplayOutcome(
        applied=applied,
        skipped_noop=skipped,
        final_root=tree.root,
        expected_final_root=last_signed_root,
        first_seq=first_seq,
        last_seq=records[-1]["seq"] if records else None,
    )


def _short(key_hex: Optional[str]) -> Optional[str]:
    """Diagnostics redaction helper: keep only a prefix of the key."""
    if not key_hex:
        return key_hex
    return key_hex[:8] + "…"
