"""Node store protocol and in-memory implementation used by the tree kernel.

The kernel only depends on a small *content-addressed* node store:
    get(h) -> stored node blob or None (None != an empty hash; empty hashes
    are virtual, derived by the crypto layer, never stored)
    put(blob) -> h

Persistence (SQLite) lives behind the same protocol in
``smt.storage.node_store``.
"""
from __future__ import annotations

from typing import Dict, Optional, Protocol


class MissingNodeError(KeyError):
    """Raised when a proof/build needs a node the store does not have.

    Distinct from "key not in tree": this is a pruned/unavailable backing
    node and the caller genuinely cannot decide.
    """


class NodeStore(Protocol):
    def get_node(self, node_hash: bytes) -> Optional[bytes]: ...
    def put_node(self, blob: bytes) -> bytes: ...


class MemoryNodeStore:
    """Content-addressed blob store; blobs hash to their own key.

    Useful for tests, for the independent verifier experiments and for
    rebuilding a tree during offline replay.
    """

    def __init__(self) -> None:
        self._blobs: Dict[bytes, bytes] = {}

    def get_node(self, node_hash: bytes) -> Optional[bytes]:
        return self._blobs.get(node_hash)

    def put_node(self, blob: bytes) -> bytes:
        from ..crypto.hashing import sha256

        node_hash = sha256(blob)
        # Content addressing: refuse to silently store a different blob
        # under an existing hash (would indicate a hash collision / bug).
        existing = self._blobs.get(node_hash)
        if existing is not None and existing != blob:
            raise RuntimeError("content-address collision in node store")
        self._blobs.setdefault(node_hash, blob)
        return node_hash

    def __len__(self) -> int:
        return len(self._blobs)

    def __contains__(self, node_hash: bytes) -> bool:
        return node_hash in self._blobs
