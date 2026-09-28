"""Fixed 256-bit-key sparse Merkle tree kernel.

Invariants
----------
* Every node is content-addressed: node id = SHA256(preimage).
* Empty subtrees are *virtual*: ``empty_at(d)`` commits to "no entries in
  this subtree", and never collides with a stored leaf/branch (different
  domain tag).  Absence of a key is therefore not represented by a leaf
  with an empty value — a stored value of b"" is a real leaf with its own
  hash.
* Nodes are kept in a canonical form so the root is a pure function of the
  (key -> value) map: a branch whose two children are empty collapses to
  the virtual empty hash; a branch with one empty child collapses to the
  other child.  Insert/delete sequences thus always converge on the same
  root (in particular: delete then re-insert restores the exact root).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..crypto.encoding import (
    HASH_BYTES,
    KEY_BITS,
    KEY_BYTES,
    TAG_BRANCH,
    TAG_LEAF,
    bit_at,
    lp,
)
from ..crypto.hashing import empty_at
from .store import MissingNodeError, NodeStore


# ---------------------------------------------------------------------------
# Blob parsing
# ---------------------------------------------------------------------------
def _read_lp(blob: bytes, off: int) -> Tuple[bytes, int]:
    if off + 2 > len(blob):
        raise ValueError("truncated length prefix")
    n = int.from_bytes(blob[off:off + 2], "big")
    off += 2
    if off + n > len(blob):
        raise ValueError("length prefix runs past end of blob")
    return blob[off:off + n], off + n


def parse_leaf(blob: bytes) -> Tuple[bytes, bytes]:
    if not blob or blob[0] != TAG_LEAF[0]:
        raise ValueError("not a leaf blob")
    key = blob[1:1 + KEY_BYTES]
    if len(key) != KEY_BYTES:
        raise ValueError("leaf blob too short for key")
    value, _ = _read_lp(blob, 1 + KEY_BYTES)
    return key, value


def parse_branch(blob: bytes) -> Tuple[bytes, bytes]:
    if not blob or blob[0] != TAG_BRANCH[0]:
        raise ValueError("not a branch blob")
    left, off = _read_lp(blob, 1)
    right, _ = _read_lp(blob, 1 + 2 + HASH_BYTES)
    if len(left) != HASH_BYTES or len(right) != HASH_BYTES:
        raise ValueError("branch children must be 32-byte hashes")
    return left, right


def _put_leaf(store: NodeStore, key: bytes, value: bytes) -> bytes:
    return store.put_node(bytes(TAG_LEAF) + key + lp(value))


def _put_branch(store: NodeStore, left: bytes, right: bytes) -> bytes:
    return store.put_node(bytes(TAG_BRANCH) + lp(left) + lp(right))


def _is_leaf_hash(store: NodeStore, node_hash: bytes) -> bool:
    blob = store.get_node(node_hash)
    if blob is None:
        raise MissingNodeError(node_hash.hex())
    return blob[0:1] == TAG_LEAF


def _canonical_branch(store: NodeStore, depth: int, left: bytes, right: bytes) -> bytes:
    """Canonical collapse rule.

    * both children empty  -> the virtual empty subtree hash (nothing stored);
    * exactly one child empty, and the other is a LEAF -> hoist that leaf.
      A leaf is safe to hoist because it binds the full 256-bit key, so a
      walker can check membership at any depth.
    * a non-empty *branch* child is never hoisted: a branch blob does not
      encode its depth and is interpreted against the key bit at the depth
      where it is reached. Hoisting it would make that bit index wrong and
      render keys unreachable.
    This rule makes the root a pure function of the (key->value) map, reached
    identically by any insert/delete order (delete+reinsert restores root).
    """
    empty_child = empty_at(depth + 1)
    if left == empty_child:
        if right == empty_child:
            return empty_at(depth)
        if _is_leaf_hash(store, right):
            return right
    elif right == empty_child and _is_leaf_hash(store, left):
        return left
    return _put_branch(store, left, right)


# ---------------------------------------------------------------------------
# Proof representation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RawStep:
    depth: int
    bit: int          # queried key's bit at this depth
    sibling: bytes    # hash of the off-path subtree (rooted at depth+1)


@dataclass(frozen=True)
class Proof:
    root: bytes
    key: bytes
    exists: bool
    terminal_depth: int
    # membership terminal (exists=True): the leaf found
    leaf_key: Optional[bytes]
    leaf_value: Optional[bytes]
    # ordered off-path steps, levels 0..terminal_depth-1
    steps: Tuple[RawStep, ...]

    def to_dict(self, compress: bool = True) -> dict:
        entries: List[dict] = []
        i = 0
        while i < len(self.steps):
            step = self.steps[i]
            is_empty = step.sibling == empty_at(step.depth + 1)
            if compress and is_empty:
                run = 1
                while (
                    i + run < len(self.steps)
                    and self.steps[i + run].depth == step.depth + run
                    and self.steps[i + run].sibling == empty_at(step.depth + run + 1)
                ):
                    run += 1
                entries.append({"kind": "empty_run", "depth": step.depth, "length": run})
                i += run
            else:
                entries.append(
                    {"kind": "sibling", "depth": step.depth, "sibling_hash": step.sibling.hex()}
                )
                i += 1

        if self.exists:
            terminal = {
                "kind": "leaf",
                "key": self.leaf_key.hex(),
                "value": self.leaf_value.hex(),
            }
        else:
            if self.leaf_key is None:
                terminal = {"kind": "empty"}
            else:
                terminal = {
                    "kind": "leaf",
                    "key": self.leaf_key.hex(),
                    "value": self.leaf_value.hex(),
                }
        return {
            "version": "smt-v1",
            "root": self.root.hex(),
            "key": self.key.hex(),
            "exists": self.exists,
            "terminal_depth": self.terminal_depth,
            "terminal": terminal,
            "steps": entries,
        }


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------
class SparseMerkleTree:
    """Mutable handle over a node store at a particular root."""

    def __init__(self, store: NodeStore, root: Optional[bytes] = None) -> None:
        self.store = store
        self.root = root if root is not None else empty_at(0)

    def with_root(self, root: bytes) -> "SparseMerkleTree":
        return SparseMerkleTree(self.store, root)

    # ----- reads -----------------------------------------------------------
    def get(self, key: bytes) -> Optional[bytes]:
        """Return stored value, or None if the key is absent.

        b"" is returned as b"", never confused with None.
        """
        h, depth = self.root, 0
        while True:
            if h == empty_at(depth):
                return None
            blob = self.store.get_node(h)
            if blob is None:
                raise MissingNodeError(h.hex())
            if blob[0:1] == TAG_LEAF:
                leaf_key, value = parse_leaf(blob)
                return value if leaf_key == key else None
            left, right = parse_branch(blob)
            h = left if bit_at(key, depth) == 0 else right
            depth += 1

    def prove(self, key: bytes, compress: bool = True) -> Proof:
        steps: List[RawStep] = []
        h, depth = self.root, 0
        while True:
            if h == empty_at(depth):
                return Proof(
                    root=self.root, key=key, exists=False, terminal_depth=depth,
                    leaf_key=None, leaf_value=None, steps=tuple(steps),
                )
            blob = self.store.get_node(h)
            if blob is None:
                raise MissingNodeError(h.hex())
            if blob[0:1] == TAG_LEAF:
                leaf_key, value = parse_leaf(blob)
                if leaf_key == key:
                    return Proof(
                        root=self.root, key=key, exists=True, terminal_depth=depth,
                        leaf_key=leaf_key, leaf_value=value, steps=tuple(steps),
                    )
                return Proof(
                    root=self.root, key=key, exists=False, terminal_depth=depth,
                    leaf_key=leaf_key, leaf_value=value, steps=tuple(steps),
                )
            left, right = parse_branch(blob)
            b = bit_at(key, depth)
            sibling = right if b == 0 else left
            steps.append(RawStep(depth=depth, bit=b, sibling=sibling))
            h = left if b == 0 else right
            depth += 1

    # ----- writes ----------------------------------------------------------
    def update(self, key: bytes, value: Optional[bytes]) -> bytes:
        """Insert/overwrite (value bytes, b"" allowed) or delete (None).

        Returns the new root and updates this handle to it.
        """
        new_root = self._update(self.root, 0, key, value)
        self.root = new_root
        return new_root

    def update_batch(self, items: List[Tuple[bytes, Optional[bytes]]]) -> bytes:
        """Deterministic batch: reject duplicate keys, apply sorted by key.

        The batch root equals the root from applying the same items one by
        one in ascending key order (asserted in the test suite).
        """
        seen = set()
        for key, _ in items:
            if key in seen:
                raise ValueError("duplicate key in batch")
            seen.add(key)
        root = self.root
        for key, value in sorted(items, key=lambda kv: kv[0]):
            root = self._update(root, 0, key, value)
        self.root = root
        return root

    def _update(self, h: bytes, depth: int, key: bytes, value: Optional[bytes]) -> bytes:
        if h == empty_at(depth):
            if value is None:
                return empty_at(depth)          # delete of absent key: no-op
            return _put_leaf(self.store, key, value)

        blob = self.store.get_node(h)
        if blob is None:
            raise MissingNodeError(h.hex())

        if blob[0:1] == TAG_LEAF:
            existing_key, _ = parse_leaf(blob)
            if existing_key == key:
                if value is None:
                    return empty_at(depth)      # deletion
                return _put_leaf(self.store, key, value)
            if value is None:
                return h                        # deleting a different key: no-op
            # Two different keys meet -> split down to their first differing bit.
            new_leaf = _put_leaf(self.store, key, value)
            return self._split(depth, key, new_leaf, existing_key, h)

        left, right = parse_branch(blob)
        b = bit_at(key, depth)
        if b == 0:
            new_left = self._update(left, depth + 1, key, value)
            return _canonical_branch(self.store, depth, new_left, right)
        new_right = self._update(right, depth + 1, key, value)
        return _canonical_branch(self.store, depth, left, new_right)

    def _split(
        self, depth: int, key: bytes, new_leaf: bytes, existing_key: bytes, existing_leaf: bytes
    ) -> bytes:
        if depth >= KEY_BITS:
            # Two distinct 256-bit keys must differ before depth 256.
            raise MissingNodeError("cannot split past key width")
        kb, eb = bit_at(key, depth), bit_at(existing_key, depth)
        if kb != eb:
            left, right = (new_leaf, existing_leaf) if kb == 0 else (existing_leaf, new_leaf)
            return _canonical_branch(self.store, depth, left, right)
        child = self._split(depth + 1, key, new_leaf, existing_key, existing_leaf)
        if kb == 0:
            return _canonical_branch(self.store, depth, child, empty_at(depth + 1))
        return _canonical_branch(self.store, depth, empty_at(depth + 1), child)
