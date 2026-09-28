"""Scope of the "remapping does not depend on input order" claim.

Claim (bounded, see README): under the fixed sort policy, the *content* of the
output — global dictionary, global codes, per-row decoded values — is invariant
under (a) batch submission order and (b) arbitrary permutation of local
dictionary codes. The claim does NOT cover:

* the *ordinal* order of batch_remaps entries (it follows submission order);
* duplicate local dictionary entries (canonicalization changes local codes by
  contract — first occurrence wins);
* changing the declared value_type or the value contents themselves.
"""
from __future__ import annotations

import itertools

from app.core.kernel import BatchInput, unify
from app.core.verify import decode_rows
from tests.fixtures import make_batch
from tests.oracle import oracle_unify


def to_kernel(raw):
    out = []
    for rb in raw:
        validity = (rb["validity"] if rb.get("validity") is not None
                    else [True] * len(rb["indices"]))
        out.append(BatchInput(rb["batch_id"], list(rb["dictionary"]),
                              list(rb["indices"]), list(validity)))
    return out


def _permute_local_codes(rb, perm):
    """Permute local dictionary codes by mapping old code -> perm[old]."""
    inv = [0] * len(perm)
    for new, old in enumerate(perm):
        inv[old] = new
    new_dict = [rb["dictionary"][old] for old in perm]
    new_indices = [inv[i] for i in rb["indices"]]
    return make_batch(rb["batch_id"], new_dict, new_indices,
                      rb.get("validity"))


def _fingerprint(result, batches_submission_ids):
    """Order-independent fingerprint keyed by batch_id: global content +
    per-batch decoded rows. Rows are sorted by batch_id so the fingerprint is
    independent of submission order; the ordinal order assertion is separate.
    """
    decoded = {d.batch_id: d.rows for d in decode_rows(result)}
    return (
        tuple(result.global_dictionary),
        result.index_width_bits,
        tuple(sorted((bid, decoded[bid]) for bid in batches_submission_ids)),
    )


def test_global_content_invariant_under_batch_permutation():
    raw = {
        "b0": make_batch("b0", ["a", "b"], [0, 1, 0]),
        "b1": make_batch("b1", ["b", "c"], [0, 1, 1]),
        "b2": make_batch("b2", ["a", "c", "d"], [2, 0, 1]),
    }
    base = unify(to_kernel([raw[k] for k in ("b0", "b1", "b2")]),
                 value_type="string")
    base_fp = _fingerprint(base, ("b0", "b1", "b2"))

    for order in itertools.permutations(("b0", "b1", "b2")):
        r = unify(to_kernel([raw[k] for k in order]), value_type="string")
        assert _fingerprint(r, order) == base_fp
        # ordinal order does follow submission (documented non-claim)
        assert tuple(x.batch_id for x in r.batch_remaps) == order


def test_global_codes_invariant_under_local_code_permutation():
    original = make_batch("b0", ["a", "b", "c"], [0, 1, 2, 0, 2])
    variants = []
    for perm in itertools.permutations(range(3)):
        variants.append(_permute_local_codes(original, list(perm)))
    base = unify(to_kernel([variants[0]]), value_type="string")
    base_rows = decode_rows(base)[0].rows
    for v in variants[1:]:
        r = unify(to_kernel([v]), value_type="string")
        assert tuple(r.global_dictionary) == tuple(base.global_dictionary)
        assert decode_rows(r)[0].rows == base_rows


def test_order_independence_agrees_with_oracle(overlapping_batches):
    rev = list(reversed(overlapping_batches))
    a = unify(to_kernel(overlapping_batches), value_type="string")
    b = unify(to_kernel(rev), value_type="string")
    assert tuple(a.global_dictionary) == tuple(b.global_dictionary)

    oracle = oracle_unify(overlapping_batches, value_type="string")
    assert tuple(a.global_dictionary) == oracle.global_dictionary


def test_order_independence_does_not_cover_value_content_change():
    """Different input content => different global dict; the claim is about
    ordering alone, never about equivalence of different datasets."""
    r1 = unify(to_kernel([make_batch("b", ["a"], [0])]), value_type="string")
    r2 = unify(to_kernel([make_batch("b", ["b"], [0])]), value_type="string")
    assert r1.global_dictionary != r2.global_dictionary
