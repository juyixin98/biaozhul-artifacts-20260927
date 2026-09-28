"""Binary Keccak Merkle tree over transaction hashes.

Pairs the Ethereum way: for an odd element at a level it is promoted
unchanged (the Yellow-paper/``trie-root`` style "copy up" used by the block
transaction-root construction over an ordered hash list). A real tree, not a
single hash, so tampering with or reordering any transaction changes the root.
"""

from __future__ import annotations

from .crypto import keccak256


def _pair_hash(a: bytes, b: bytes) -> bytes:
    return keccak256(a + b)


def merkle_root(leaves: list[bytes]) -> bytes:
    """Return the 32-byte Merkle root of ordered 32-byte leaves."""
    if not leaves:
        return keccak256(b"")  # empty block: deterministic, non-null root
    level = list(leaves)
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_pair_hash(level[i], level[i + 1]))
            else:
                nxt.append(level[i])  # odd node promoted unchanged
        level = nxt
    return level[0]
