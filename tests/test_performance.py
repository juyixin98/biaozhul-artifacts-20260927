"""Performance guardrails on the DAG core (kept modest so it runs in CI).

These do not assert exact timing (machine dependent); they assert the service
stays comfortably responsive at its sentence-scale input cap and completes a
larger batch input, while preserving correctness on the long input.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.algorithm import segment
from app.lexicon import LexiconVersion, WordEntry

_SEED = Path(__file__).resolve().parent.parent / "data" / "seed_lexicon.json"


@pytest.fixture(scope="module")
def lexicon() -> LexiconVersion:
    data = json.loads(_SEED.read_text(encoding="utf-8"))
    return LexiconVersion.build(
        1, [WordEntry(w["word"], w["freq"]) for w in data["words"]]
    )


def test_sentence_scale_is_fast(lexicon: LexiconVersion) -> None:
    text = "研究生命" * 100  # 400 chars, deliberately ambiguous
    t0 = time.perf_counter()
    result = segment(text, lexicon)
    elapsed = time.perf_counter() - t0
    # 400 chars should be a few milliseconds on any CI machine; allow slack.
    assert elapsed < 2.0
    # correctness on the long input: coverage is complete and continuous
    assert result.segments[0].raw_start == 0
    assert result.segments[-1].raw_end == len(text)
    assert "".join(s.surface for s in result.segments) == result.normalized_text


def test_large_batch_completes_and_covers(lexicon: LexiconVersion) -> None:
    # Above the interactive cap but still a valid library-level workload.
    text = "研究生命" * 1000  # 4000 chars
    t0 = time.perf_counter()
    result = segment(text, lexicon)
    elapsed = time.perf_counter() - t0
    assert elapsed < 10.0
    assert result.segments[-1].raw_end == len(text)
    for a, b in zip(result.segments, result.segments[1:]):
        assert b.raw_start == a.raw_end
    # repeated optimum: tokens alternate exactly 研究 / 生命
    assert result.segments[0].surface == "研究"
    assert result.segments[1].surface == "生命"
