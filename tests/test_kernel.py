"""Unit tests for the sparse Merkle kernel, asserting concrete results."""
from __future__ import annotations

import json

import pytest

from smt.crypto import empty_at, hash_leaf
from smt.kernel import MemoryNodeStore, SparseMerkleTree, Verdict, verify_proof
from smt.kernel.tree import MissingNodeError


def test_empty_tree_root_is_level_zero(mem_tree):
    _, tree = mem_tree
    assert tree.root == empty_at(0)


def test_present_empty_value_distinct_from_absence(mem_tree, keys):
    _, tree = mem_tree
    tree.update(keys["ka"], b"")
    assert tree.get(keys["ka"]) == b""          # present
    assert tree.get(keys["abs_far"]) is None    # absent

    # the leaf commitment of value b"" differs from the empty subtree hash
    assert hash_leaf(keys["ka"], b"") != empty_at(256)

    # deleting an empty-string value actually removes the key
    tree.update(keys["ka"], None)
    assert tree.get(keys["ka"]) is None
    assert tree.root == empty_at(0)


def test_delete_of_absent_key_is_noop(mem_tree, keys):
    _, tree = mem_tree
    before = tree.root
    tree.update(keys["ka"], None)
    assert tree.root == before


def test_long_common_prefix_structure(mem_tree, keys):
    """ka/kb share 248 bits; they must split exactly at depth 248."""
    _, tree = mem_tree
    tree.update_batch([(keys["ka"], b"alpha"), (keys["kb"], b"beta")])
    assert tree.get(keys["ka"]) == b"alpha"
    assert tree.get(keys["kb"]) == b"beta"

    pka = tree.prove(keys["ka"]).to_dict()
    pkb = tree.prove(keys["kb"]).to_dict()
    assert pka["terminal_depth"] == 256
    assert pkb["terminal_depth"] == 256

    # Compression: one run covers the 255 shared empty siblings, plus one
    # real sibling (the other leaf) at depth 255.
    assert [e for e in pka["steps"] if e["kind"] == "empty_run"] == [
        {"kind": "empty_run", "depth": 0, "length": 255}
    ]
    assert [e["kind"] for e in pka["steps"]].count("sibling") == 1
    assert verify_proof(pka).ok
    assert verify_proof(pkb).ok

    # An uncompressed proof has 256 explicit siblings and verifies identically
    full = tree.prove(keys["ka"]).to_dict(compress=False)
    assert len(full["steps"]) == 256
    assert all(e["kind"] == "sibling" for e in full["steps"])
    assert verify_proof(full).ok


def test_delete_restore_returns_exact_root(mem_tree, keys):
    store, tree = mem_tree
    r1 = tree.update(keys["ka"], b"alpha")
    r2 = tree.update(keys["kb"], b"beta")
    r3 = tree.update(keys["kc"], b"gamma")

    def fresh_root(items):
        s = MemoryNodeStore()
        tt = SparseMerkleTree(s)
        return tt.update_batch(list(items))

    # removing a key leaves the canonical root of the remaining set;
    # restoring it returns the exact previous root.
    tree.update(keys["kc"], None)
    assert tree.root == r2 == fresh_root([(keys["ka"], b"alpha"), (keys["kb"], b"beta")])
    tree.update(keys["kc"], b"gamma")
    assert tree.root == r3

    tree.update(keys["ka"], None)
    assert tree.root == fresh_root([(keys["kb"], b"beta"), (keys["kc"], b"gamma")])
    tree.update(keys["ka"], b"alpha")
    assert tree.root == r3

    tree.update(keys["ka"], None)
    tree.update(keys["kb"], None)
    assert tree.root == fresh_root([(keys["kc"], b"gamma")])
    tree.update(keys["kc"], None)
    assert tree.root == empty_at(0)
    tree.update(keys["ka"], b"alpha")
    assert tree.root == r1
    tree.update(keys["kb"], b"beta")
    assert tree.root == r2
    tree.update(keys["kc"], b"gamma")
    assert tree.root == r3


def test_delete_absent_key_keeps_other_entries(mem_tree, keys):
    _, tree = mem_tree
    tree.update(keys["ka"], b"alpha")
    root_before = tree.root
    tree.update(keys["kb"], None)  # absent, shares long prefix
    assert tree.root == root_before
    assert tree.get(keys["ka"]) == b"alpha"


def test_overwrite_changes_root_and_value(mem_tree, keys):
    _, tree = mem_tree
    r1 = tree.update(keys["ka"], b"alpha")
    r2 = tree.update(keys["ka"], b"ALPHA2")
    assert r1 != r2
    assert tree.get(keys["ka"]) == b"ALPHA2"
    assert verify_proof(tree.prove(keys["ka"]).to_dict()).ok


def test_nonmembership_two_kinds(mem_tree, keys):
    _, tree = mem_tree
    tree.update_batch([(keys["ka"], b"alpha"), (keys["kb"], b"beta"), (keys["kc"], b"gamma")])

    # absence inside the shared-prefix region, proved by an empty subtree.
    # ...ab02 diverges from ...ab00/...ab01 at bit 254, so the empty slot is
    # reached at depth 255.
    p_sub = tree.prove(keys["abs_sub"]).to_dict()
    assert p_sub["exists"] is False
    assert p_sub["terminal"]["kind"] == "empty"
    assert p_sub["terminal_depth"] == 255
    assert verify_proof(p_sub).ok

    # absence proved by an empty subtree at a SHALLOW depth: no inserted key
    # begins with 0x1..., so the queried key walks only a few levels before
    # hitting the empty subtree.
    p_far = tree.prove(keys["abs_far"]).to_dict()
    assert p_far["exists"] is False
    assert p_far["terminal"]["kind"] == "empty"
    assert p_far["terminal_depth"] == 4
    assert verify_proof(p_far).ok

    # a diverging-LEAF non-membership: kc (ff..) is the only inserted key
    # whose first bit is 1. A queried key fe.. has bit0=1 (kc's side) but
    # bit1=0 (empty), so it reaches kc's hoisted leaf as the off-path
    # terminal at depth 1.
    k_div = bytes.fromhex("fe" + "00" * 31)
    p_leaf = tree.prove(k_div).to_dict()
    assert p_leaf["exists"] is False
    assert p_leaf["terminal"]["kind"] == "leaf"
    assert p_leaf["terminal"]["key"] == keys["kc"].hex()
    assert p_leaf["terminal"]["key"] != p_leaf["key"]
    assert p_leaf["terminal_depth"] == 1
    assert verify_proof(p_leaf).ok

    # empty tree proves non-membership with zero steps
    empty_tree = SparseMerkleTree(MemoryNodeStore())
    p0 = empty_tree.prove(keys["abs_far"]).to_dict()
    assert p0["steps"] == [] and p0["terminal"] == {"kind": "empty"}
    assert verify_proof(p0).ok


def test_batch_root_equals_sorted_sequential_root(keys):
    items = [(keys["ka"], b"alpha"), (keys["kb"], b"beta"), (keys["kc"], b"gamma")]
    store = MemoryNodeStore()
    batch_tree = SparseMerkleTree(store)
    batch_root = batch_tree.update_batch(items)

    store2 = MemoryNodeStore()
    seq_tree = SparseMerkleTree(store2)
    for k, v in sorted(items, key=lambda kv: kv[0]):
        seq_tree.update(k, v)
    assert batch_root == seq_tree.root

    # and every permutation converges on the same root
    import itertools
    roots = set()
    for perm in itertools.permutations(items):
        s = MemoryNodeStore()
        t = SparseMerkleTree(s)
        for k, v in perm:
            t.update(k, v)
        roots.add(t.root)
    assert roots == {batch_root}


def test_batch_rejects_duplicate_keys(mem_tree, keys):
    _, tree = mem_tree
    with pytest.raises(ValueError):
        tree.update_batch([(keys["ka"], b"a"), (keys["ka"], b"b")])


# ---------------------------------------------------------------------------
# Tampering: every mutation class must produce the specific failure category
# ---------------------------------------------------------------------------
def _valid_membership(mem_tree, keys):
    """Single-key tree: root is the leaf itself (no path steps)."""
    _, tree = mem_tree
    tree.update(keys["ka"], b"alpha")
    return tree.prove(keys["ka"]).to_dict()


def _long_prefix_membership(mem_tree, keys):
    """ka+kb split at bit 248 -> one compressed run + two real siblings."""
    _, tree = mem_tree
    tree.update_batch([(keys["ka"], b"alpha"), (keys["kb"], b"beta")])
    return tree.prove(keys["ka"]).to_dict()


def test_tamper_root_gives_root_mismatch(mem_tree, keys):
    p = _valid_membership(mem_tree, keys)
    p["root"] = ("ff" * 32)
    r = verify_proof(p)
    assert r.verdict is Verdict.ROOT_MISMATCH


def test_tamper_value_gives_root_mismatch(mem_tree, keys):
    p = _valid_membership(mem_tree, keys)
    raw = bytes.fromhex(p["terminal"]["value"]) + b"!"
    p["terminal"]["value"] = raw.hex()
    r = verify_proof(p)
    assert r.verdict is Verdict.ROOT_MISMATCH


def test_tamper_terminal_key_gives_key_mismatch(mem_tree, keys):
    p = _valid_membership(mem_tree, keys)
    p["terminal"]["key"] = ("01" * 32)
    r = verify_proof(p)
    assert r.verdict is Verdict.KEY_MISMATCH


def test_contradictory_exists_flag_gives_terminal_invalid(mem_tree, keys):
    p = _valid_membership(mem_tree, keys)
    p["exists"] = False
    r = verify_proof(p)
    assert r.verdict is Verdict.PREFIX_MISMATCH  # terminal key == queried key


def test_tamper_compressed_run_length_gives_step_invalid(mem_tree, keys):
    _, tree = mem_tree
    tree.update_batch([(keys["ka"], b"alpha"), (keys["kb"], b"beta")])
    p = tree.prove(keys["ka"]).to_dict()
    # shrink the 255-long run so steps no longer reach the terminal depth
    run = next(e for e in p["steps"] if e["kind"] == "empty_run")
    run["length"] = run["length"] - 1
    r = verify_proof(p)
    assert r.verdict is Verdict.STEP_INVALID


def test_tamper_compressed_run_depth_gives_step_invalid(mem_tree, keys):
    _, tree = mem_tree
    tree.update_batch([(keys["ka"], b"alpha"), (keys["kb"], b"beta")])
    p = tree.prove(keys["ka"]).to_dict()
    run = next(e for e in p["steps"] if e["kind"] == "empty_run")
    run["depth"] = 5
    r = verify_proof(p)
    assert r.verdict is Verdict.STEP_INVALID


def test_overlapping_run_entries_give_step_invalid(mem_tree, keys):
    _, tree = mem_tree
    tree.update_batch([(keys["ka"], b"alpha"), (keys["kb"], b"beta")])
    p = tree.prove(keys["ka"]).to_dict()
    # duplicate the run: the second now starts at a non-zero covered depth
    p["steps"].append(dict(p["steps"][0]))
    r = verify_proof(p)
    assert r.verdict is Verdict.STEP_INVALID


def test_tamper_sibling_hash_gives_root_mismatch(mem_tree, keys):
    _, tree = mem_tree
    tree.update_batch([(keys["ka"], b"a"), (keys["kc"], b"c")])
    p = tree.prove(keys["ka"]).to_dict()
    sib = next(e for e in p["steps"] if e["kind"] == "sibling")
    sib["sibling_hash"] = ("ab" * 32)
    r = verify_proof(p)
    assert r.verdict is Verdict.ROOT_MISMATCH


def test_nonmembership_prefix_mismatch_category(mem_tree, keys):
    # A valid non-membership for k_div (fe..) ends at kc's leaf (ff..) which
    # shares only bit 0. Tamper the terminal to ka's leaf (00..), which is on
    # the other side at bit 0 and therefore NOT on k_div's path: the verifier
    # must reject with PREFIX_MISMATCH before the root fold masks it.
    _, tree = mem_tree
    tree.update_batch([
        (keys["ka"], b"alpha"), (keys["kb"], b"beta"), (keys["kc"], b"gamma"),
    ])
    k_div = bytes.fromhex("fe" + "00" * 31)
    p = tree.prove(k_div).to_dict()
    assert p["terminal"]["kind"] == "leaf" and p["terminal"]["key"] == keys["kc"].hex()
    p["terminal"] = {"kind": "leaf", "key": keys["ka"].hex(), "value": b"alpha".hex()}
    r = verify_proof(p)
    assert r.verdict is Verdict.PREFIX_MISMATCH


def test_malformed_proofs(mem_tree, keys):
    p = _valid_membership(mem_tree, keys)
    assert verify_proof("nope").verdict is Verdict.MALFORMED
    bad = dict(p)
    bad["version"] = "smt-v9"
    assert verify_proof(bad).verdict is Verdict.MALFORMED
    bad = dict(p)
    bad["root"] = "xyz"
    assert verify_proof(bad).verdict is Verdict.MALFORMED


def test_missing_node_is_distinct_from_absence(keys):
    store = MemoryNodeStore()
    # claim a random root that the store has no blob for and is not a virtual empty
    phantom = bytes.fromhex("cd" * 32)
    ghost = SparseMerkleTree(store, phantom)
    with pytest.raises(MissingNodeError):
        ghost.get(keys["ka"])
    with pytest.raises(MissingNodeError):
        ghost.prove(keys["ka"])


def test_hoisted_root_leaf_nonmembership_at_depth_zero(mem_tree, keys):
    # Single key: the root is the hoisted leaf itself. An absent key must get a
    # depth-0 diverging-leaf non-membership proof (zero steps).
    _, tree = mem_tree
    tree.update(keys["ka"], b"alpha")
    p = tree.prove(keys["kc"]).to_dict()
    assert p["exists"] is False
    assert p["terminal_depth"] == 0
    assert p["terminal"] == {"kind": "leaf", "key": keys["ka"].hex(), "value": b"alpha".hex()}
    assert p["steps"] == []
    assert verify_proof(p).ok


def test_proof_serializes_as_json(mem_tree, keys):
    _, tree = mem_tree
    tree.update(keys["ka"], b"alpha")
    p = tree.prove(keys["ka"]).to_dict()
    again = json.loads(json.dumps(p))
    assert verify_proof(again).ok
