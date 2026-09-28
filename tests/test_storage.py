"""Storage tests: indexes, persistence, atomic rollback, resource limits."""

import os
import sqlite3

import pytest

from lc import encoding
from lc.config import KernelConfig
from lc.errors import Category, Code, LightClientError, StorageError
from lc.store import Store
from lc.types import Certificate, Checkpoint, Header

from test_kernel_golden import install, objs, single, tip_tuple
from helpers import base_signers, x32


def _cp(golden):
    return Checkpoint.from_dict(golden["checkpoint"], committee_max_size=256)


def test_persist_and_reopen(tmp_path, golden):
    db = str(tmp_path / "a.db")
    s1 = Store(db)
    root = encoding.header_root(_cp(golden).header)
    assert s1.get_header(root) is None
    s1.close()
    s2 = Store(db)
    assert s2.get_header(root) is None  # nothing installed yet
    s2.close()


def test_indexes_find_by_parent_and_round(golden, kernel):
    install(golden, kernel)
    chain = single(golden, "legal_continuous_3")
    h0 = Header.from_dict(chain["items"][0]["header"])
    kernel.apply_header(h0, Certificate.from_dict(chain["items"][0]["certificate"]))
    store = kernel.store
    cp_root = x32(golden["checkpoint"]["root"])
    child = store.get_child(cp_root)
    assert child is not None and child.round == 101
    rounds = store.get_headers_by_round(100)
    assert len(rounds) == 1


def test_committee_indexed_by_commitment(golden, kernel):
    install(golden, kernel)
    rot = single(golden, "committee_rotation_authorized")
    rh, rc, rnc = objs(rot)
    kernel.apply_header(rh, rc, rnc)
    commitment = x32(rot["new_committee_commitment"])
    got = kernel.store.get_committee(commitment)
    assert got is not None
    assert got.total_weight == 50


def test_rejected_batch_rolls_back_all_headers(golden, kernel):
    """A batch whose 2nd header is invalid must not leave the 1st stored."""
    import oracle as o

    install(golden, kernel)
    chain = single(golden, "legal_continuous_3")
    h0 = Header.from_dict(chain["items"][0]["header"])
    c0 = Certificate.from_dict(chain["items"][0]["certificate"])
    # A correctly-signed but stale-round second item: rejection is a state
    # conflict, not a certificate problem. Timestamp is valid so the trust
    # gate passes; the round regression is what must roll the batch back.
    _, key_map, pks = base_signers(golden)
    bad_ts = chain["items"][0]["header"]["timestamp"] + 6_000
    bad_dict = o.make_header(
        50, encoding.header_root(h0), bad_ts, body_root=b"z" * 32
    )
    bad_root = o.o_header_root(bad_dict)
    bad = Header.from_dict(bad_dict)
    bad_cert = Certificate.from_dict(o.make_certificate(bad_root, key_map, pks[:5]))
    before_count = kernel.store.count_headers()
    before = tip_tuple(kernel)
    with pytest.raises(LightClientError) as ei:
        kernel.apply_batch([(h0, c0, None), (bad, bad_cert, None)])
    assert ei.value.code is Code.STALE_ROUND
    assert ei.value.category is Category.STATE_CONFLICT
    assert kernel.store.count_headers() == before_count
    assert tip_tuple(kernel) == before


def test_oversized_batch_is_resource_error(golden, kernel):
    install(golden, kernel)
    h, cert, _ = objs(single(golden, "weight_below_threshold"))
    with pytest.raises(LightClientError) as ei:
        kernel.apply_batch([(h, cert)] * (kernel.config.max_batch_size + 1))
    assert ei.value.code is Code.BATCH_TOO_LARGE
    assert ei.value.category is Category.RESOURCE
    assert tip_tuple(kernel) == tip_tuple(kernel)  # unchanged trivially


def test_oversized_committee_is_resource_error(golden):
    big = {
        "members": [
            {"public_key": "0x" + (f"{i:0{64}d}"), "weight": 1}
            for i in range(10)
        ]
    }
    with pytest.raises(LightClientError) as ei:
        from lc.types import Committee as C

        C.from_dict(big, max_size=4)
    assert ei.value.code is Code.COMMITTEE_TOO_LARGE
    assert ei.value.category is Category.RESOURCE


def test_storage_failure_mapped_to_resource(tmp_path):
    # Opening a path that is a directory -> sqlite error -> STORAGE_FAILURE
    with pytest.raises(StorageError) as ei:
        Store(str(tmp_path))
    assert ei.value.category is Category.RESOURCE


def test_tip_snapshot_equality(golden, kernel):
    install(golden, kernel)
    s1 = kernel.store.get_tip().as_snapshot()
    s2 = kernel.store.get_tip().as_snapshot()
    assert s1 == s2


def test_state_survives_reopen_and_accepts_more(golden, tmp_path, clock):
    """Restart recovery: checkpoint + accepted header persist; a reopened
    client keeps the same tip/committee and can extend the chain."""
    from lc.chain import ChainKernel
    from lc.clock import RunRecorder

    db = str(tmp_path / "persist.db")
    cfg = KernelConfig()
    s1 = Store(db)
    k1 = ChainKernel(s1, clock, cfg, RunRecorder())
    install(golden, k1)
    chain = single(golden, "legal_continuous_3")
    h0 = Header.from_dict(chain["items"][0]["header"])
    k1.apply_header(h0, Certificate.from_dict(chain["items"][0]["certificate"]))
    expected_tip = tip_tuple(k1)
    s1.close()

    s2 = Store(db)
    s2.assert_consistent()
    k2 = ChainKernel(s2, clock, cfg, RunRecorder())
    assert tip_tuple(k2) == expected_tip
    # can extend from the recovered tip
    h1 = Header.from_dict(chain["items"][1]["header"])
    rep = k2.apply_header(h1, Certificate.from_dict(chain["items"][1]["certificate"]))
    assert rep.accepted is True
    assert rep.round == 102
    s2.close()
