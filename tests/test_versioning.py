"""Tests for complete-version publishing and in-flight version pinning."""
from __future__ import annotations

import pytest

from app.storage.models import (
    CODE_COST_INVALID,
    CODE_DUPLICATE_SURFACE,
    CODE_EMPTY_BATCH,
    CODE_FREQUENCY_INVALID,
    CODE_NORMALIZED_EMPTY,
    CODE_SURFACE_EMPTY,
    PublishValidationError,
)
from app.storage.registry import VersionNotFoundError


V1 = [
    {"surface": "研究", "frequency": 100},
    {"surface": "生命", "frequency": 90},
    {"surface": "研究生", "frequency": 50},
]

V2 = [
    {"surface": "研究", "frequency": 100},
    {"surface": "生命", "frequency": 90},
    {"surface": "研究生", "frequency": 50000},  # dominates under log-prob cost
    {"surface": "命", "frequency": 15},
]


def test_publish_returns_immutable_snapshot(registry):
    snapshot = registry.publish(V1)
    assert snapshot.word_count() == 3
    assert snapshot.trie.max_key_length == 3
    # Snapshot is frozen per request: re-publish does not mutate it.
    registry.publish(V2)
    assert snapshot.word_count() == 3


def test_pinned_version_unchanged_after_new_publish(registry, diag):
    v1 = registry.publish(V1)
    v2 = registry.publish(V2)

    # Resolve+pin at request start, then "later" publish cannot move it.
    pinned = registry.resolve(v1.version)
    current = registry.resolve(None)
    assert current.version == v2.version
    assert pinned.version == v1.version

    r_old = registry.segment("研究生命", diag, version_ref=v1.version)
    assert r_old.version == v1.version
    assert list(r_old.best.surfaces) == ["研究", "生命"]

    r_new = registry.segment("研究生命", diag)
    assert r_new.version == v2.version
    assert list(r_new.best.surfaces) == ["研究生", "命"]


def test_inflight_request_keeps_version_across_concurrent_publish(registry, diag):
    v1 = registry.publish(V1)
    # A long-running request would hold its snapshot object; emulate by
    # resolving first and using the pinned ref afterwards.
    snapshot = registry.resolve(None)
    for i in range(3):
        registry.publish(V2 if i % 2 == 0 else V1)
    # The object captured at request start is still the original snapshot.
    assert snapshot.version == v1.version
    assert snapshot.word_count() == 3


def test_unknown_version_rejected(registry, diag):
    registry.publish(V1)
    with pytest.raises(VersionNotFoundError):
        registry.resolve("v-does-not-exist")


def test_all_versions_listed_and_retained(registry):
    a = registry.publish(V1, note="a")
    b = registry.publish(V2, note="b")
    versions = {v.version: v for v in registry.list_versions()}
    assert set(versions) == {a.version, b.version}
    assert versions[b.version].is_current is True
    assert versions[a.version].is_current is False
    # Old version still resolvable and readable.
    assert registry.resolve(a.version).word_count() == 3


def test_republish_same_content_is_idempotent(registry):
    a = registry.publish(V1)
    b = registry.publish(V1)
    assert a.version == b.version  # content-addressed id


# --------------------------------------------------------------------------- #
# Validation failure categories
# --------------------------------------------------------------------------- #
def _codes(payload, registry):
    with pytest.raises(PublishValidationError) as exc:
        registry.prepare(payload)
    return [i["code"] for i in exc.value.issues]


def test_empty_batch_rejected(registry):
    assert CODE_EMPTY_BATCH in _codes([], registry)


def test_not_a_list_rejected(registry):
    with pytest.raises(PublishValidationError) as exc:
        registry.prepare({"surface": "x"})
    assert exc.value.issues[0]["code"] == "not_a_list"


def test_empty_and_missing_fields(registry):
    codes = _codes([
        {"surface": ""},
        {"frequency": 5},
        {"surface": "ok", "frequency": 1},
    ], registry)
    assert CODE_SURFACE_EMPTY in codes
    assert "surface_missing" in codes


def test_bad_frequency_and_cost(registry):
    codes = _codes([
        {"surface": "a", "frequency": -3},
        {"surface": "b", "frequency": "x"},
        {"surface": "c", "cost": 0},
        {"surface": "d", "cost": -1.0},
    ], registry)
    assert codes.count(CODE_FREQUENCY_INVALID) == 2
    assert codes.count(CODE_COST_INVALID) == 2


def test_normalized_empty_surface_rejected(registry):
    codes = _codes([{"surface": "​"}], registry)  # only a ZWSP
    assert CODE_NORMALIZED_EMPTY in codes


def test_duplicate_normalized_keys_rejected(registry):
    codes = _codes([
        {"surface": "ﬁ", "frequency": 5},   # normalizes to "fi"
        {"surface": "fi", "frequency": 5},
    ], registry)
    assert CODE_DUPLICATE_SURFACE in codes


def test_failed_publish_creates_no_version(registry):
    with pytest.raises(PublishValidationError):
        registry.publish([{"surface": "a", "frequency": 0}])
    assert registry.current() is None


def test_frequency_costs_are_log_probabilities(registry):
    snap = registry.publish([
        {"surface": "rare", "frequency": 1},
        {"surface": "common", "frequency": 99},
    ])
    rare = snap.trie.get("rare")
    common = snap.trie.get("common")
    assert rare.cost > common.cost
    # log(100/1) and log(100/99)
    assert rare.cost == pytest.approx(4.60517, abs=1e-4)
    assert common.cost == pytest.approx(0.0100503, abs=1e-4)
