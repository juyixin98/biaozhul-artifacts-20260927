"""Word-cost model.

Dictionary entries carry a non-negative integer ``frequency``.  The cost of
dictionary word *w* is the negative log unigram probability (MLE):

    cost(w) = log(F / f_w),   F = sum of frequencies used as frequency words

so higher frequency means lower cost and the cost of concatenating words
equals the (negative log) joint unigram probability.

An entry may instead supply an explicit ``cost`` (used directly).  Explicit
costs never enter the frequency denominator ``F``.  Explicit costs exist so
that dictionaries can encode priors/penalties directly and so tests can pin
exact arithmetic; they are never *required* to obtain a correct result.

All costs are positive: they are clipped at ``min_word_cost`` so a zero or
negative value cannot create negative cycles in the DAG.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FrequencyBasis:
    """Statistics used to turn raw frequencies into log costs."""

    total_frequency: int
    word_count: int

    def word_cost(self, frequency: int, min_word_cost: float = 0.01) -> float:
        if self.total_frequency <= 0 or frequency <= 0:
            # Degenerate dictionary: treat every frequency word uniformly.
            return 0.0
        return max(math.log(self.total_frequency / frequency), min_word_cost)
