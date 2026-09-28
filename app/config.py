"""Configuration loading and the cost model.

The cost model is explicit and validated up front: every edit cost must be a
finite, non-negative number. Non-negativity is *required* by the algorithm
(the A* search and the admissible pruning lower bounds both assume it),
so a negative or NaN weight makes the service refuse to start rather than
silently producing unsound distances.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

INF = float("inf")
EPS = 1e-9


@dataclass(frozen=True)
class CostModel:
    """Non-negative weighted edit costs.

    - insertion/deletion are keyed by the single character.
    - substitution by ``"a:b"`` (source ``a`` -> target ``b``): asymmetric.
    - transposition by ``"a:b"`` meaning source pair ``a b`` becomes ``b a``.
      ``"b:a"`` is a different key, so transposition costs may be asymmetric too.
    """

    insert_default: float = 1.0
    delete_default: float = 1.0
    substitute_default: float = 1.0
    transpose_default: float = 1.0
    insert: dict[str, float] = field(default_factory=dict)
    delete: dict[str, float] = field(default_factory=dict)
    substitute: dict[str, float] = field(default_factory=dict)
    transpose: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = [
            ("insert_default", self.insert_default),
            ("delete_default", self.delete_default),
            ("substitute_default", self.substitute_default),
            ("transpose_default", self.transpose_default),
        ]
        values += [(f"insert[{k!r}]", v) for k, v in self.insert.items()]
        values += [(f"delete[{k!r}]", v) for k, v in self.delete.items()]
        values += [(f"substitute[{k!r}]", v) for k, v in self.substitute.items()]
        values += [(f"transpose[{k!r}]", v) for k, v in self.transpose.items()]
        for name, value in values:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"cost {name} must be a number, got {type(value).__name__}")
            fv = float(value)
            if math.isnan(fv) or math.isinf(fv) or fv < 0.0:
                raise ValueError(f"cost {name}={value} must be finite and non-negative")

    def insert_cost(self, ch: str) -> float:
        return float(self.insert.get(ch, self.insert_default))

    def delete_cost(self, ch: str) -> float:
        return float(self.delete.get(ch, self.delete_default))

    def substitute_cost(self, src: str, dst: str) -> float:
        if src == dst:
            return 0.0
        return float(self.substitute.get(f"{src}:{dst}", self.substitute_default))

    def transpose_cost(self, left: str, right: str) -> float:
        # Cost of swapping adjacent source chars `left right` -> `right left`.
        return float(self.transpose.get(f"{left}:{right}", self.transpose_default))

    @property
    def min_insert(self) -> float:
        return float(min([self.insert_default, *self.insert.values()]))

    @property
    def min_delete(self) -> float:
        return float(min([self.delete_default, *self.delete.values()]))

    @property
    def min_indel(self) -> float:
        return min(self.min_insert, self.min_delete)

    @property
    def min_edit(self) -> float:
        return min(self.min_indel, self.substitute_default, self.transpose_default,
                   *self.substitute.values(), *self.transpose.values())


@dataclass(frozen=True)
class Limits:
    max_query_chars: int = 64
    max_query_tokens: int = 8
    max_candidates_scored: int = 5000
    max_results_per_token: int = 10
    max_search_nodes: int = 60_000
    default_threshold: float = 2.0


@dataclass(frozen=True)
class Settings:
    db_path: str
    seed_file: str
    alphabet: str
    limits: Limits
    costs: CostModel

    @property
    def alphabet_set(self) -> frozenset[str]:
        return frozenset(self.alphabet)


def load_settings(path: str | Path = "config/default.json") -> Settings:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    costs = CostModel(**raw["costs"])
    limits = Limits(**raw["limits"])
    settings = Settings(
        db_path=raw["db_path"],
        seed_file=raw["seed_file"],
        alphabet=raw["alphabet"],
        limits=limits,
        costs=costs,
    )
    # The alphabet must be closed under the cost tables: every keyed character
    # has to be representable after normalization.
    for ch in _keyed_characters(costs):
        if ch not in settings.alphabet_set:
            raise ValueError(f"cost table references character {ch!r} absent from alphabet")
    return settings


def _keyed_characters(costs: CostModel) -> list[str]:
    chars: list[str] = []
    for table in (costs.insert, costs.delete):
        chars.extend(table.keys())
    for table in (costs.substitute, costs.transpose):
        for key in table:
            a, _, b = key.partition(":")
            chars.append(a)
            chars.append(b)
    return chars
