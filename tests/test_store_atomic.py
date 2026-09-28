"""Atomicity: after ANY rejection, the on-disk trusted state is unchanged,
even when the store is reopened from the database file (not just in memory)."""

from __future__ import annotations

import pytest

from lightclient import codec
from lightclient.config import LightClientConfig
from lightclient.errors import ErrorCode
from lightclient.fixtures.builder import ChainBuilder
from lightclient.kernel import LightClientKernel
from lightclient.store import Store


def _make(db_path, builder):
    return LightClientKernel(
        Store(db_path), LightClientConfig(), builder.checkpoint_pub, run_id="atom"
    )


def test_rejected_header_leaves_no_row_and_tip_survives_reopen(tmp_path):
    builder = ChainBuilder()
    _g, env = builder.genesis()
    k = _make(str(tmp_path / "lc.db"), builder)
    k.bootstrap(env)

    good = builder.add_block(signer_labels=["c0-a", "c0-b"])
    k.apply_header(good.header, good.certificate)
    good_digest = codec.header_digest(good.header)

    # rejected block: below quorum
    bad = builder.add_block(signer_labels=["c0-a"])
    with pytest.raises(Exception) as ei:
        k.apply_header(bad.header, bad.certificate)
    assert ei.value.code == ErrorCode.WEIGHT_BELOW_QUORUM

    # rejected header digest must not exist anywhere
    bad_digest = codec.header_digest(bad.header)
    assert not k.store.has_header(bad_digest)
    assert k.store.has_header(good_digest)
    # no certificate row for the rejected header
    rows = k.store._conn.execute(
        "SELECT 1 FROM certificates WHERE header_digest=?", (bad_digest.hex(),)
    ).fetchall()
    assert rows == []
    k.store.close()

    # reopen the same file: tip is exactly the last accepted block
    k2 = _make(str(tmp_path / "lc.db"), builder)
    assert k2.is_initialized()
    assert k2.tip().digest == good_digest
    assert not k2.store.has_header(bad_digest)


def test_rejected_committee_announcement_not_persisted_across_reopen(tmp_path):
    builder = ChainBuilder()
    _g, env = builder.genesis()
    k = _make(str(tmp_path / "lc2.db"), builder)
    k.bootstrap(env)

    c1 = builder.add_committee(1, [("c1-a", 1), ("c1-b", 1)])
    announce = builder.add_block(signer_labels=["c0-a"], next_committee=c1.committee)
    with pytest.raises(Exception) as ei:
        k.apply_header(announce.header, announce.certificate)
    assert ei.value.code == ErrorCode.WEIGHT_BELOW_QUORUM
    cid = codec.committee_id(c1.committee)
    assert not k.store.has_committee(cid)
    k.store.close()

    k2 = _make(str(tmp_path / "lc2.db"), builder)
    assert not k2.store.has_committee(cid)
    assert k2.tip().height == 0


def test_audit_row_written_even_on_reject_and_persisted(tmp_path):
    builder = ChainBuilder()
    _g, env = builder.genesis()
    k = _make(str(tmp_path / "lc3.db"), builder)
    k.bootstrap(env)
    blk = builder.add_block(signer_labels=["c0-a"])
    with pytest.raises(Exception):
        k.apply_header(blk.header, blk.certificate)
    k.store.close()

    # reopen and read the persisted audit trail
    store = Store(str(tmp_path / "lc3.db"))
    entries = store.list_audit(50)
    rejects = [e for e in entries if e["result"] == "rejected"]
    assert len(rejects) == 1
    rec = rejects[0]
    assert rec["error_code"] == ErrorCode.WEIGHT_BELOW_QUORUM.value
    assert rec["error_category"] == "state"
    assert rec["run_id"] == "atom"
    # the audited record explicitly asserts the decision invariant
    assert rec["detail"]["state_unchanged"] is True
    assert rec["detail"]["tip_before"] == rec["detail"]["tip_after"]
    store.close()
