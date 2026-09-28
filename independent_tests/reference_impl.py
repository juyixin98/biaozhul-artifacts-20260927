"""Independent reference implementation used ONLY by the test suite.

Nothing in this module imports the production package ``smt``.  It
re-derives every hash and verifies every proof from the written spec in
``docs/spec.md``, in an intentionally different style (nested tuples + a
dict store, explicit recursion, no production types).  If the production
kernel and this reference agree on hard-coded known answers, the answers
are not "the system grading its own homework".

Tree model (independently specified):
    leaf   = SHA256(b'L' || key32 || u16(len val) || val)
    branch = SHA256(b'B' || left32 || right32)
    empty slot at level 256: SHA256(b'E' || u16be(256))
    empty subtree at level d:
        SHA256(b'E' || u16be(d) || empty[d+1] || empty[d+1])
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Tuple

KEY_BYTES = 32
KEY_BITS = 256

# Tag bytes match the production domain tags (the spec fixes them);
# everything else here is written from scratch.
LEAF_TAG = b"\x00"
BRANCH_TAG = b"\x01"
EMPTY_TAG = b"\x02smt-v1-empty"


def H(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def leaf_hash(key: bytes, value: bytes) -> bytes:
    return H(LEAF_TAG + key + len(value).to_bytes(2, "big") + value)


def branch_hash(left: bytes, right: bytes) -> bytes:
    return H(BRANCH_TAG + len(left).to_bytes(2, "big") + left
             + len(right).to_bytes(2, "big") + right)


def empty_table() -> Tuple[bytes, ...]:
    t: List[bytes] = [b""] * (KEY_BITS + 1)
    t[KEY_BITS] = H(EMPTY_TAG + KEY_BITS.to_bytes(2, "big"))
    for d in range(KEY_BITS - 1, -1, -1):
        t[d] = H(EMPTY_TAG + d.to_bytes(2, "big") + t[d + 1] + t[d + 1])
    return tuple(t)


EMPTY = empty_table()


def kbit(key: bytes, depth: int) -> int:
    return (key[depth >> 3] >> (7 - (depth & 7))) & 1


# A node is ("empty", depth) | ("leaf", key, value) | ("branch", left, right)
Node = tuple
Store = Dict[bytes, Node]


def nid(node: Node) -> bytes:
    tag = node[0]
    if tag == "empty":
        return EMPTY[node[1]]
    if tag == "leaf":
        return leaf_hash(node[1], node[2])
    return branch_hash(nid(node[1]), nid(node[2]))


def insert(node: Node, depth: int, key: bytes, value: Optional[bytes]) -> Node:
    """Functional insert/delete returning the new node (canonical collapses)."""
    if node[0] == "empty":
        if value is None:
            return node
        return ("leaf", key, value)
    if node[0] == "leaf":
        ek = node[1]
        if ek == key:
            return ("empty", depth) if value is None else ("leaf", key, value)
        if value is None:
            return node
        return _split(depth, ("leaf", key, value), node)
    left, right = node[1], node[2]
    if kbit(key, depth) == 0:
        left = insert(left, depth + 1, key, value)
    else:
        right = insert(right, depth + 1, key, value)
    return _join(depth, left, right)


def _join(depth: int, left: Node, right: Node) -> Node:
    # both empty -> empty
    if left[0] == "empty" and right[0] == "empty":
        return ("empty", depth)
    # single child hoisted only when it is a leaf (leaves carry the full key)
    if left[0] == "empty" and right[0] == "leaf":
        return right
    if right[0] == "empty" and left[0] == "leaf":
        return left
    return ("branch", left, right)


def _split(depth: int, new_leaf: Node, old_leaf: Node) -> Node:
    nk, ok = new_leaf[1], old_leaf[1]
    if kbit(nk, depth) != kbit(ok, depth):
        if kbit(nk, depth) == 0:
            return _join(depth, new_leaf, old_leaf)
        return _join(depth, old_leaf, new_leaf)
    child = _split(depth + 1, new_leaf, old_leaf)
    if kbit(nk, depth) == 0:
        return _join(depth, child, ("empty", depth + 1))
    return _join(depth, ("empty", depth + 1), child)


def lookup(node: Node, key: bytes, depth: int = 0) -> Tuple[bool, Optional[bytes]]:
    if node[0] == "empty":
        return False, None
    if node[0] == "leaf":
        return (True, node[2]) if node[1] == key else (False, None)
    child = node[1] if kbit(key, depth) == 0 else node[2]
    return lookup(child, key, depth + 1)


def root_of(items: List[Tuple[bytes, Optional[bytes]]]) -> bytes:
    """Reference root for a set of (key, value-or-None) pairs, applied sorted."""
    node: Node = ("empty", 0)
    for key, value in sorted(items, key=lambda kv: kv[0]):
        node = insert(node, 0, key, value)
    return nid(node)


# ---------------------------------------------------------------------------
# Independent proof checker.  Accepts the same JSON proof shape as the API.
# Returns (ok: bool, category: str, detail: str).
# ---------------------------------------------------------------------------
def check(proof: dict) -> Tuple[bool, str, str]:
    try:
        if proof.get("version") != "smt-v1":
            return False, "malformed", "version"
        root = bytes.fromhex(proof["root"])
        key = bytes.fromhex(proof["key"])
        if len(root) != 32 or len(key) != 32:
            return False, "malformed", "hash width"
        exists = proof["exists"]
        td = proof["terminal_depth"]
        if not isinstance(td, int) or not 0 <= td <= 256:
            return False, "step_invalid", "terminal_depth"

        term = proof["terminal"]
        if term["kind"] == "empty":
            if exists:
                return False, "terminal_invalid", "exists with empty terminal"
            cur = EMPTY[td]
        else:
            tk = bytes.fromhex(term["key"])
            tv = bytes.fromhex(term["value"])
            cur = leaf_hash(tk, tv)
            if exists:
                if tk != key:
                    return False, "key_mismatch", "membership binds other key"
            else:
                if tk == key:
                    return False, "prefix_mismatch", "absence terminal has queried key"
                for d in range(td):
                    if kbit(tk, d) != kbit(key, d):
                        return False, "prefix_mismatch", f"diverges before depth {td}"

        # Expand the (possibly compressed) path into one sibling per level;
        # then fold from the terminal UP toward the root.
        sibling_at: Dict[int, bytes] = {}
        depth = 0
        for i, e in enumerate(proof["steps"]):
            if e["kind"] == "sibling":
                if e["depth"] != depth:
                    return False, "step_invalid", f"sibling gap at step {i}"
                sib = bytes.fromhex(e["sibling_hash"])
                if len(sib) != 32:
                    return False, "malformed", "sibling width"
                sibling_at[depth] = sib
                depth += 1
            elif e["kind"] == "empty_run":
                if e["depth"] != depth:
                    return False, "step_invalid", f"run gap at step {i}"
                step = int(e["length"])
                if step <= 0:
                    return False, "step_invalid", "run length"
                for off in range(step):
                    d = depth + off
                    sibling_at[d] = EMPTY[d + 1]
                depth += step
            else:
                return False, "malformed", "step kind"

        if depth != td:
            return False, "step_invalid", "steps do not reach terminal depth"

        for d in range(td - 1, -1, -1):
            sibling = sibling_at[d]
            if kbit(key, d) == 0:
                cur = branch_hash(cur, sibling)
            else:
                cur = branch_hash(sibling, cur)
        if cur != root:
            return False, "root_mismatch", "recomputed root differs"
        return True, "valid", "accepted"
    except (KeyError, TypeError, ValueError) as exc:
        return False, "malformed", f"parse error: {type(exc).__name__}: {exc}"
