"""State service: applies updates through the kernel and records a signed
append-only journal plus revision history on top of the indexed SQLite store.

The service is the only component that writes.  Reads (get/prove) may target
any historical root.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..crypto.encoding import normalize_key, normalize_value
from ..crypto.hashing import empty_at, sign_payload
from ..kernel.tree import SparseMerkleTree
from ..kernel.verifier import VerificationResult, verify_proof
from ..storage.node_store import SqliteNodeStore


class ServiceError(Exception):
    """Request-level failure with a stable category code."""

    def __init__(self, category: str, message: str, **context) -> None:
        super().__init__(message)
        self.category = category
        self.context = context


@dataclass
class UpdateEffect:
    kind: str                  # "set" or "delete"
    key: bytes
    value: Optional[bytes]
    prev_root: bytes
    new_root: bytes
    journal_seq: int


class StateService:
    def __init__(self, store: SqliteNodeStore, hmac_key: str) -> None:
        self.store = store
        self.hmac_key = hmac_key
        self._lock = threading.Lock()  # serialize committing writers
        rev = store.latest_revision()
        root = rev[1] if rev is not None else empty_at(0)
        self._root = root
        self._rev_seq = rev[0] if rev is not None else 0
        if rev is None:
            # Anchor the genesis empty root as revision 1 for stable history.
            self._rev_seq = store.record_revision(empty_at(0), note="genesis empty root")

    # ----- views -----
    @property
    def root(self) -> bytes:
        return self._root

    @property
    def revision(self) -> int:
        return self._rev_seq

    def tree_at(self, root: Optional[bytes] = None) -> SparseMerkleTree:
        return SparseMerkleTree(self.store, root if root is not None else self._root)

    def get_value(self, key_hex: str, root: Optional[bytes] = None) -> Tuple[bool, Optional[bytes]]:
        """Returns (exists, value).  Absence -> (False, None); b"" -> (True,b"")."""
        key = normalize_key(key_hex)
        try:
            value = self.tree_at(root).get(key)
        except KeyError as exc:
            raise ServiceError(
                "unknown_root",
                "root has no backing nodes in this store",
                root=(root or self._root).hex(),
            ) from exc
        return (True, value) if value is not None else (False, None)

    def issue_proof(self, key_hex: str, root: Optional[bytes] = None, compress: bool = True) -> dict:
        key = normalize_key(key_hex)
        try:
            return self.tree_at(root).prove(key).to_dict(compress=compress)
        except KeyError:
            raise ServiceError(
                "unknown_root",
                "root has no backing nodes in this store",
                root=(root or self._root).hex(),
            )

    def check_proof(self, proof: dict) -> VerificationResult:
        return verify_proof(proof)

    # ----- writes -----
    def update_one(self, key_hex: str, value: Optional[bytes | str]) -> UpdateEffect:
        return self.apply_batch([(key_hex, value)])[0]

    def apply_batch(self, items: List[Tuple[str, Optional[bytes | str]]]) -> List[UpdateEffect]:
        # Normalize + reject duplicates before touching the tree.
        normalized = []
        seen = set()
        for key_hex, raw_value in items:
            key = normalize_key(key_hex)
            value = normalize_value(raw_value)  # None => delete
            if value is not None and len(value) > 0xFFFF:
                # leaf values share the 16-bit length-prefixed encoding
                raise ServiceError(
                    "value_too_large",
                    "leaf value exceeds the 65535-byte encoding limit",
                    key=key.hex(), byte_length=len(value),
                )
            if key in seen:
                raise ServiceError("duplicate_key", "batch contains the same key twice", key=key.hex())
            seen.add(key)
            normalized.append((key, value))
        normalized.sort(key=lambda kv: kv[0])  # deterministic batch rule

        effects: List[UpdateEffect] = []
        with self._lock:
            prev_root = self._root
            tree = SparseMerkleTree(self.store, prev_root)
            try:
                for key, value in normalized:
                    start = tree.root
                    new_root = tree.update(key, value)
                    if new_root == start:
                        continue  # delete of absent key: no journal entry
                    kind = "delete" if value is None else "set"
                    payload = {
                        "version": "smt-v1",
                        "kind": kind,
                        "key_hex": key.hex(),
                        "value_hex": None if value is None else value.hex(),
                        "prev_root": start.hex(),
                        "new_root": new_root.hex(),
                    }
                    signature = sign_payload(payload, self.hmac_key)
                    seq = self.store.append_journal(
                        kind=kind,
                        key_hex=key.hex(),
                        value_hex=None if value is None else value.hex(),
                        prev_root=start,
                        new_root=new_root,
                        payload=payload,
                        signature_hex=signature,
                    )
                    payload["seq"] = seq
                    effects.append(
                        UpdateEffect(
                            kind=kind, key=key, value=value,
                            prev_root=start, new_root=new_root, journal_seq=seq,
                        )
                    )
            except KeyError as exc:
                raise ServiceError("missing_node", "update path references an unavailable node") from exc

            if tree.root != prev_root:
                self._rev_seq = self.store.record_revision(
                    tree.root, note=f"batch:{len(effects)}"
                )
            self._root = tree.root
        return effects

    # ----- history export -----
    def export_journal(self, after_seq: int = 0) -> List[dict]:
        rows = self.store.journal_rows(after_seq=after_seq)
        for row in rows:
            row.pop("payload", None)  # canonical bytes live in payload_json
        return rows
