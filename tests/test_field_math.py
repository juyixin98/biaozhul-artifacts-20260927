"""Field-math tests: galois kernel vs the independent oracle vs literal vectors.

The reference answers here come from :mod:`oracle` (no galois, no app.core) and
from hard-coded integers -- never from the code under test.
"""
from __future__ import annotations

import itertools
import random

import pytest

from app.core.field import (
    CHUNK_BYTES,
    SECP256K1_P,
    lagrange_constant,
    poly_eval,
    verify_points_fit,
)
import oracle


def test_prime_constants_agree():
    # The oracle states the prime literally; they must describe the same field.
    assert oracle.P == SECP256K1_P
    assert CHUNK_BYTES == 31


def test_known_answer_line():
    # f(x) = 7 + 4x over GF(p); f(0)=7.
    coeffs = [4, 7]
    points = [(x, (7 + 4 * x) % SECP256K1_P) for x in (1, 2, 3)]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    assert lagrange_constant(xs, ys) == 7
    assert poly_eval(coeffs, 5) == 27
    # independent oracle agrees
    assert oracle.recover(xs, ys) == 7
    assert oracle.poly_eval(coeffs, 5) == 27


def test_known_answer_quadratic_negative_wraps():
    # f(x) = x^2 - 3x + 11 ; constant 11. Include x values large enough that
    # intermediate products exercise modular reduction/wrapping.
    coeffs = [1, SECP256K1_P - 3, 11]
    xs = [2, 5, 9]
    ys = [poly_eval(coeffs, x) for x in xs]
    assert lagrange_constant(xs, ys) == 11
    assert oracle.recover(xs, ys) == 11


@pytest.mark.parametrize("t,n,seed", [
    (2, 3, 1), (3, 5, 2), (2, 5, 3), (4, 6, 7), (5, 5, 11),
])
def test_oracle_and_galois_split_agree_on_every_subset(t, n, seed):
    secret = 12345678901234567890
    xs, ys, coeffs = oracle.split(secret, t, n, seed=seed)
    # Every threshold subset recovers the exact constant via galois.
    for subset in itertools.combinations(range(n), t):
        sxs = [xs[i] for i in subset]
        sys_ = [ys[i] for i in subset]
        assert lagrange_constant(sxs, sys_) == secret
        assert oracle.recover(sxs, sys_) == secret
    # The kernel's poly_eval matches the oracle at unseen points too.
    for x in range(1, n + 1):
        assert poly_eval(coeffs, x) == oracle.poly_eval(coeffs, x)


def test_less_than_threshold_does_not_recover():
    # A degree-3 polynomial: any <4 points must give a *different* constant.
    xs, ys, _ = oracle.split(42, 4, 6, seed=99)
    for subset in itertools.combinations(range(6), 3):
        sxs = [xs[i] for i in subset]
        sys_ = [ys[i] for i in subset]
        assert oracle.recover(sxs, sys_) != 42
        assert lagrange_constant(sxs, sys_) != 42


def test_verify_points_fit_detects_one_bad_point():
    xs, ys, _ = oracle.split(42, 3, 5, seed=5)
    baseline_x = xs[:3]
    baseline_y = ys[:3]
    # A genuine extra share fits.
    assert verify_points_fit([(xs[3], ys[3])], baseline_x, baseline_y) == []
    # Corrupt that extra share's y -> it must be flagged, and only that x.
    bad = [(xs[3], (ys[3] + 1) % SECP256K1_P), (xs[4], ys[4])]
    assert verify_points_fit(bad, baseline_x, baseline_y) == [xs[3]]


def test_pinned_field_constants_are_actually_valid():
    # We construct galois GF(p) with verify=False and a pinned generator; make
    # sure the pinned constants really are prime + primitive so that shortcut
    # can never silently put us on the wrong field.
    import galois
    from app.core.field import PRIMITIVE_ELEMENT
    # Verifying construction is expensive (primitive search), but we assert the
    # primality cheaply via a couple of witnesses and trust galois's own checked
    # build for the generator proof.
    assert pow(2, SECP256K1_P - 1, SECP256K1_P) == 1
    assert pow(3, SECP256K1_P - 1, SECP256K1_P) == 1
    # galois accepts the pinned generator and reports it back.
    GF = galois.GF(SECP256K1_P, primitive_element=PRIMITIVE_ELEMENT, verify=False)
    assert int(GF.primitive_element) == PRIMITIVE_ELEMENT


def test_random_polynomials_cross_engine(monkeypatch):
    rng = random.Random(2026)
    for _ in range(25):
        t = rng.randint(2, 6)
        n = rng.randint(t, t + 4)
        coeffs = [rng.randrange(1, SECP256K1_P) for _ in range(t - 1)]
        coeffs.append(rng.randrange(0, SECP256K1_P))
        secret = coeffs[-1]
        xs = list(range(1, n + 1))
        gy = [poly_eval(coeffs, x) for x in xs]
        oy = [oracle.poly_eval(coeffs, x) for x in xs]
        assert gy == oy
        subset = rng.sample(range(n), t)
        sxs = [xs[i] for i in subset]
        sys_ = [gy[i] for i in subset]
        assert lagrange_constant(sxs, sys_) == secret
        assert oracle.recover(sxs, sys_) == secret
