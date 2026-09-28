"""Randomized differential fuzzing: production kernel vs independent reference.

Runs many random insert/overwrite/delete sequences on a small universe of
keys (which collide at shallow depths), and after EVERY operation asserts:

* production root == reference root;
* a membership/non-membership proof for every sampled key verifies under the
  production verifier AND under the independent checker, both agreeing with
  an independent reference lookup.
"""
from __future__ import annotations

import random

import pytest

from smt.kernel import MemoryNodeStore, SparseMerkleTree, verify_proof
from reference_impl import insert as ref_insert, lookup as ref_lookup, nid as ref_nid

pytestmark = pytest.mark.slow


def _key(small: int) -> bytes:
    # keys differ mostly in the LOW bits -> shallow splits, leaf hoisting and
    # compressed runs are all exercised
    return small.to_bytes(32, "big")


@pytest.mark.parametrize("seed", range(40))
def test_random_mutations_match_reference(seed):
    rng = random.Random(seed)
    universe = [_key(i) for i in range(12)]
    sample_queries = universe + [_key(100 + rng.randrange(50)) for _ in range(4)]

    store = MemoryNodeStore()
    tree = SparseMerkleTree(store)
    ref: tuple = ("empty", 0)
    state: dict[bytes, bytes] = {}

    for _ in range(60):
        k = rng.choice(universe)
        action = rng.choice(["set", "overwrite", "delete"])
        if action in ("set", "overwrite") or k in state:
            if action == "delete":
                value = None
            else:
                value = rng.choice([b"v", b"", b"value-" + str(rng.randrange(5)).encode()])
        else:
            value = b"v"

        tree.update(k, value)
        ref = ref_insert(ref, 0, k, value)
        if value is None:
            state.pop(k, None)
        else:
            state[k] = value

        # roots agree
        assert tree.root == ref_nid(ref), f"seed {seed}: root divergence"

        # get() agrees with a plain dict model
        for qk in universe:
            got = tree.get(qk)
            assert got == state.get(qk), f"seed {seed}: get mismatch"

        # proofs for sampled keys agree between both verifiers + reference
        for qk in sample_queries:
            proof = tree.prove(qk).to_dict()
            res = verify_proof(proof)
            assert res.ok, f"seed {seed}: production rejected {res.verdict.value}"
            ref_ok, ref_cat, ref_detail = _ref_check(proof)
            assert ref_ok, f"seed {seed}: reference rejected {ref_cat}: {ref_detail}"

            exists, value = ref_lookup(ref, qk)
            assert proof["exists"] is exists


def _ref_check(proof: dict):
    from reference_impl import check
    return check(proof)
