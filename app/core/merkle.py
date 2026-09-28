"""Two-level labeled Merkle trees.

Level 1 (within a record): leaves are field commitments, ordered by the
canonical field position. Level 2 (the batch): leaves are record roots,
ordered by record index.

Both trees use identical, explicit rules:

* leaves carry the *index* and the declared *leaf count* inside their hash, so
  a proof cannot be replayed at another position or against a padded tree;
* internal nodes hash the two child hashes as exactly two framed parts;
* odd nodes at a level are promoted by duplicating the last node (the
  duplication happens at hash input level -- a stored duplicate hash is
  distinguishable by level and framing and cannot be confused with a leaf);
* an empty tree has a dedicated labeled empty marker.
"""
from __future__ import annotations

from app.core.errors import MerklePathMismatch, ProofMalformed
from app.security.hashing import (
    LBL_FIELD_EMPTY,
    LBL_FIELD_LEAF,
    LBL_FIELD_NODE,
    LBL_RECORD_EMPTY,
    LBL_RECORD_LEAF,
    LBL_RECORD_NODE,
    labeled_hash,
)


def _labels(kind: str) -> tuple[str, str, str]:
    if kind == "field":
        return LBL_FIELD_LEAF, LBL_FIELD_NODE, LBL_FIELD_EMPTY
    if kind == "record":
        return LBL_RECORD_LEAF, LBL_RECORD_NODE, LBL_RECORD_EMPTY
    raise ProofMalformed(f"unknown tree kind {kind!r}")


def leaf_hash(kind: str, digest_name: str, index: int, count: int, node: bytes) -> bytes:
    leaf_lbl, _, _ = _labels(kind)
    return labeled_hash(
        leaf_lbl,
        digest_name,
        index.to_bytes(8, "big"),
        count.to_bytes(8, "big"),
        node,
    )


def _node_hash(kind: str, digest_name: str, left: bytes, right: bytes) -> bytes:
    _, node_lbl, _ = _labels(kind)
    return labeled_hash(node_lbl, digest_name, left, right)


def empty_root(kind: str, digest_name: str) -> bytes:
    _, _, empty_lbl = _labels(kind)
    return labeled_hash(empty_lbl, digest_name, b"")


def build_levels(
    kind: str, digest_name: str, leaves: list[bytes]
) -> tuple[bytes, list[list[bytes]]]:
    """Return ``(root, levels)`` where ``levels[0]`` are the labeled leaves.

    Empty input yields the labeled empty root and no levels.
    """
    if not leaves:
        return empty_root(kind, digest_name), []
    count = len(leaves)
    level = [leaf_hash(kind, digest_name, i, count, h) for i, h in enumerate(leaves)]
    levels = [level]
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else level[i]
            nxt.append(_node_hash(kind, digest_name, left, right))
        level = nxt
        levels.append(level)
    return level[0], levels


def authentication_path(
    levels: list[list[bytes]], index: int
) -> list[bytes | None]:
    """Sibling hashes from the leaf level toward the root.

    ``None`` marks a duplicated-last promotion: the sibling is the node
    itself. Encoding the marker explicitly (JSON ``null``) prevents a prover
    from supplying an arbitrary "missing sibling".
    """
    if not levels:
        raise MerklePathMismatch("cannot build a path over an empty tree")
    if not 0 <= index < len(levels[0]):
        raise MerklePathMismatch(
            f"leaf index {index} out of range 0..{len(levels[0]) - 1}"
        )
    siblings: list[bytes | None] = []
    pos = index
    for level in levels[:-1]:
        if pos % 2 == 0:
            sib_pos = pos + 1
            if sib_pos < len(level):
                siblings.append(level[sib_pos])
            else:
                siblings.append(None)  # duplicate-last
        else:
            siblings.append(level[pos - 1])
        pos //= 2
    return siblings


def verify_path(
    kind: str,
    digest_name: str,
    *,
    index: int,
    count: int,
    leaf_node: bytes,
    siblings: list[bytes | None],
    expected_root: bytes,
) -> None:
    """Raise :class:`MerklePathMismatch` unless the path authenticates.

    Identity checks (index/count ranges, path length) run before any hashing
    so swapped identities fail with a precise category.
    """
    if index < 0 or count <= 0:
        raise MerklePathMismatch("index must be >= 0 and count must be >= 1")
    if index >= count:
        raise MerklePathMismatch(
            f"leaf index {index} is outside declared leaf count {count}"
        )
    expected_len = (count - 1).bit_length()
    if len(siblings) != expected_len:
        raise MerklePathMismatch(
            f"authentication path has {len(siblings)} siblings, expected "
            f"{expected_len} for {count} leaves"
        )

    current = leaf_hash(kind, digest_name, index, count, leaf_node)
    pos = index
    remaining = count
    for level_no, sib in enumerate(siblings):
        if pos % 2 == 0:
            # Even index: we are the left child.
            is_last_pair = pos + 1 >= remaining
            if is_last_pair:
                if sib is not None:
                    raise MerklePathMismatch(
                        f"level {level_no}: duplicated-last slot must carry a "
                        "null marker, not a sibling hash"
                    )
                right = current
            else:
                if sib is None:
                    raise MerklePathMismatch(
                        f"level {level_no}: missing sibling for non-padded node"
                    )
                right = sib
            current = _node_hash(kind, digest_name, current, right)
        else:
            if sib is None:
                raise MerklePathMismatch(
                    f"level {level_no}: odd-indexed node requires a real left "
                    "sibling"
                )
            current = _node_hash(kind, digest_name, sib, current)
        pos //= 2
        remaining = (remaining + 1) // 2

    if current != expected_root:
        raise MerklePathMismatch(
            f"recomputed root {current.hex()[:16]}... does not match "
            f"expected {expected_root.hex()[:16]}..."
        )
