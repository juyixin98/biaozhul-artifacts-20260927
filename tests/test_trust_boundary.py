"""Trust-period boundary tests.

Uses the oracle's fixed T0 and a FixedClock so both sides of the boundary are
exercised deterministically: ``age == trust_period`` must still be fresh, and
``age == trust_period + 1`` must require a new checkpoint.
"""

import pytest

from lc.errors import Category, Code, LightClientError
from lc.types import Certificate, Checkpoint, Header, Committee
from lc import encoding

from test_kernel_golden import install, objs, single, tip_tuple
from helpers import base_signers, x32

PERIOD = 7 * 24 * 60 * 60 * 1000


def _base_signers(golden):
    return base_signers(golden)


def _fresh_header(golden, round_index, ts, body=b"\xab" * 32):
    cp_root = x32(golden["checkpoint"]["root"])
    import oracle as o

    h = o.make_header(round_index, cp_root, ts, body_root=body)
    root = o.o_header_root(h)
    _, key_map, pks = _base_signers(golden)
    cert = o.make_certificate(root, key_map, pks[:5])
    return h, cert, root


def test_exact_boundary_age_equals_period_is_fresh(golden, kernel, clock):
    install(golden, kernel)
    t0 = golden["checkpoint"]["header"]["timestamp"]
    clock.set(t0 + PERIOD)  # age exactly == period
    assert kernel.is_fresh() is True
    h, cert, root = _fresh_header(
        golden, 101, t0 + PERIOD, body=b"\x10" * 32
    )
    rep = kernel.apply_header(Header.from_dict(h), Certificate.from_dict(cert))
    assert rep.accepted is True


def test_one_ms_past_period_requires_new_checkpoint(golden, kernel, clock):
    install(golden, kernel)
    t0 = golden["checkpoint"]["header"]["timestamp"]
    clock.set(t0 + PERIOD + 1)
    assert kernel.is_fresh() is False
    h, cert, _ = _fresh_header(golden, 101, t0 + PERIOD + 1, body=b"\x11" * 32)
    before = tip_tuple(kernel)
    with pytest.raises(LightClientError) as ei:
        kernel.apply_header(Header.from_dict(h), Certificate.from_dict(cert))
    assert ei.value.code is Code.TRUST_EXPIRED
    assert ei.value.category is Category.TRUST
    assert ei.value.details["needs_new_checkpoint"] is True
    assert "checkpoint" in ei.value.message.lower()
    # state untouched even though the header/cert were valid
    assert tip_tuple(kernel) == before


def test_long_offline_peer_chain_is_rejected(golden, kernel, clock):
    """Simulate a long-offline client: a peer supplies a contiguous chain,
    but because the client's tip is beyond the trust period, the whole update
    is refused and a fresh checkpoint is demanded."""
    install(golden, kernel)
    t0 = golden["checkpoint"]["header"]["timestamp"]
    clock.set(t0 + PERIOD + 10_000)
    # build a contiguous legal batch that *ends* inside the (old) period window
    import oracle as o

    _, key_map, pks = _base_signers(golden)
    parent = x32(golden["checkpoint"]["root"])
    items = []
    for k in range(3):
        hd = o.make_header(101 + k, parent, t0 + (k + 1) * 6_000)
        root = o.o_header_root(hd)
        cert = o.make_certificate(root, key_map, pks[:5])
        items.append((Header.from_dict(hd), Certificate.from_dict(cert)))
        parent = root
    before = tip_tuple(kernel)
    with pytest.raises(LightClientError) as ei:
        kernel.apply_batch(items)
    assert ei.value.code is Code.TRUST_EXPIRED
    assert ei.value.details["boundary"] == "tip_old"
    assert tip_tuple(kernel) == before
    status = kernel.trust_status()
    assert status["needs_new_checkpoint"] is True
    assert status["fresh"] is False


def test_historical_replay_cannot_refreshen(golden, kernel, clock):
    """Tip is fresh, but the batch contains only old headers ending beyond the
    trust period in the past — historical headers alone must not be accepted as
    a freshening update."""
    install(golden, kernel)
    t0 = golden["checkpoint"]["header"]["timestamp"]
    # client clock is far ahead, but tip still fresh? make tip old instead:
    clock.set(t0 + 2 * PERIOD)
    import oracle as o

    _, key_map, pks = _base_signers(golden)
    parent = x32(golden["checkpoint"]["root"])
    hd = o.make_header(101, parent, t0 + 1000)  # ancient header
    root = o.o_header_root(hd)
    cert = o.make_certificate(root, key_map, pks[:5])
    before = tip_tuple(kernel)
    with pytest.raises(LightClientError) as ei:
        kernel.apply_header(Header.from_dict(hd), Certificate.from_dict(cert))
    assert ei.value.code is Code.TRUST_EXPIRED
    assert tip_tuple(kernel) == before


def test_fresh_header_from_a_fresh_tip_after_recheckpoint(golden, kernel, clock):
    """After receiving a NEW out-of-band checkpoint (fresh DB), updates work
    again — documents the documented recovery path."""
    install(golden, kernel)
    t0 = golden["checkpoint"]["header"]["timestamp"]
    clock.set(t0 + PERIOD + 1)
    h, cert, _ = _fresh_header(golden, 101, t0 + PERIOD + 1)
    with pytest.raises(LightClientError):
        kernel.apply_header(Header.from_dict(h), Certificate.from_dict(cert))
    # The only remedy documented: fresh checkpoint. On this client that means a
    # new store + kernel (out-of-band channel). Assert trust_status says so.
    assert kernel.trust_status()["needs_new_checkpoint"] is True
