"""Version store: atomic whole-version publishing and frozen pins."""
from __future__ import annotations

import json

import pytest

from app.lexicon import WordEntry
from app.store import VersionNotFoundError, VersionStore

from .oracle import SEED_WORDS


def _entries(words):
    return [WordEntry(w, f) for w, f in words.items()]


def test_publish_creates_monotonic_versions(store: VersionStore):
    v1 = store.publish(_entries({"a": 10}), note="one")
    v2 = store.publish(_entries({"a": 10, "b": 20}), note="two")
    assert v1.version_id == 1
    assert v2.version_id == 2
    assert store.latest_version_id() == 2
    listed = store.list_versions()
    assert [r["version_id"] for r in listed] == [1, 2]
    assert listed[0]["word_count"] == 1
    assert listed[1]["word_count"] == 2
    assert v1.checksum != v2.checksum


def test_published_versions_are_frozen_and_independent(store: VersionStore):
    store.publish(_entries({"研究": 100}))
    store.publish(_entries({"研究": 100, "生命": 50}))
    v1 = store.load(1)
    v2 = store.load(2)
    assert v1.word_count == 1
    assert v2.word_count == 2
    # the old version object is unaffected by the later publish
    assert "生命" not in v1.costs
    assert "生命" in v2.costs
    # immutable
    with pytest.raises(Exception):
        v1.entries = ()  # type: ignore[misc]


def test_load_missing_version_raises(store: VersionStore):
    store.publish(_entries({"a": 1}))
    with pytest.raises(VersionNotFoundError):
        store.load(99)


def test_load_latest_empty(store: VersionStore):
    assert store.load_latest() is None
    assert store.latest_version_id() is None


def test_publish_is_atomic_on_invalid_payload(store: VersionStore):
    store.publish(_entries({"a": 1}))
    with pytest.raises(ValueError):
        # word longer than cap -> build fails before SQL writes
        store.publish([WordEntry("x" * 100, 1)])
    # no partial version appeared
    assert store.latest_version_id() == 1
    rows = store.list_versions()
    assert len(rows) == 1


def test_publish_normalizes_and_dedupes(store: VersionStore):
    # full-width form and its folded ASCII form collide after normalization
    v = store.publish([WordEntry("ABC", 10), WordEntry("ＡＢＣ", 20)])
    assert v.word_count == 1
    # higher frequency is kept
    assert v.entries[0].word == "ABC"
    assert v.entries[0].freq == 20


def test_negative_freq_rejected(store: VersionStore):
    with pytest.raises(ValueError):
        store.publish([WordEntry("a", -1)])
    assert store.latest_version_id() is None


def test_seed_is_idempotent(store: VersionStore, tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"words": [{"word": "a", "freq": 1}]}), encoding="utf-8")
    first = store.seed_from_json(path)
    second = store.seed_from_json(path)
    assert first is not None and first.version_id == 1
    assert second is None  # already has data
    assert store.latest_version_id() == 1


def test_checksum_is_deterministic_and_detects_content_change(store: VersionStore):
    from app.lexicon import LexiconVersion

    # rebuilding the same (version, content) gives an identical checksum
    assert LexiconVersion.build(1, _entries({"a": 10})).checksum == LexiconVersion.build(
        1, _entries({"a": 10})
    ).checksum
    # and any frequency change changes it
    assert LexiconVersion.build(1, _entries({"a": 10})).checksum != LexiconVersion.build(
        1, _entries({"a": 11})
    ).checksum
    # published versions embed their version id, so they remain distinct
    c1 = store.publish(_entries({"a": 10})).checksum
    c2 = store.publish(_entries({"a": 10})).checksum
    assert c1 != c2
