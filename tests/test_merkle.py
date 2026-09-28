"""Unit tests for the labeled Merkle trees and domain separation."""
from __future__ import annotations

import pytest

from app.core.errors import MerklePathMismatch
from app.core.merkle import (
    authentication_path,
    build_levels,
    empty_root,
    leaf_hash,
    verify_path,
)
from app.security.hashing import labeled_hash

D = "sha256"


def _leaves(n: int) -> list[bytes]:
    return [labeled_hash("test-node", D, i.to_bytes(2, "big")) for i in range(n)]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 9, 16, 17])
def test_every_leaf_authenticates_for_various_sizes(n):
    leaves = _leaves(n)
    root, levels = build_levels("field", D, leaves)
    for i in range(n):
        sibs = authentication_path(levels, i)
        verify_path(
            "field", D, index=i, count=n, leaf_node=leaves[i],
            siblings=sibs, expected_root=root,
        )


def test_empty_tree_uses_labeled_marker():
    root, levels = build_levels("record", D, [])
    assert levels == []
    assert root == empty_root("record", D)


def test_wrong_index_rejected():
    leaves = _leaves(4)
    root, levels = build_levels("field", D, leaves)
    sibs = authentication_path(levels, 1)
    with pytest.raises(MerklePathMismatch):
        verify_path(
            "field", D, index=2, count=4, leaf_node=leaves[1],
            siblings=sibs, expected_root=root,
        )


def test_declared_count_is_bound_into_leaves():
    # Same node bytes but declared in a 3-leaf vs 4-leaf tree hash differently.
    h = b"\x11" * 32
    leaf3 = leaf_hash("field", D, 0, 3, h)
    leaf4 = leaf_hash("field", D, 0, 4, h)
    assert leaf3 != leaf4


def test_path_length_mismatch_rejected():
    leaves = _leaves(4)
    root, levels = build_levels("field", D, leaves)
    sibs = authentication_path(levels, 0)
    with pytest.raises(MerklePathMismatch):
        verify_path(
            "field", D, index=0, count=5, leaf_node=leaves[0],
            siblings=sibs, expected_root=root,
        )


def test_padded_slot_requires_null_marker():
    # 3 leaves: index 2 is promoted by duplication; a supplied sibling there
    # must be rejected.
    leaves = _leaves(3)
    root, levels = build_levels("field", D, leaves)
    sibs = authentication_path(levels, 2)
    assert sibs[0] is None  # explicit null marker
    tampered = [b"\x22" * 32] + sibs[1:]
    with pytest.raises(MerklePathMismatch):
        verify_path(
            "field", D, index=2, count=3, leaf_node=leaves[2],
            siblings=tampered, expected_root=root,
        )


def test_field_and_record_trees_are_domain_separated():
    leaves = _leaves(2)
    root_field, _ = build_levels("field", D, leaves)
    root_record, _ = build_levels("record", D, leaves)
    assert root_field != root_record
