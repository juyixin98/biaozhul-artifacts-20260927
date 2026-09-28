"""Independent reference oracle for tests.

This is a *separate, deliberately simple* implementation of the expected
unification behavior. It imports nothing from ``app.core.kernel`` and does not
share merge, width-selection or remap code with the service — it exists so the
tests assert concrete expected values produced independently of the code under
test (rather than the kernel re-deriving its own answers).

Input here is raw JSON-like data; typing follows the declared value_type the
same way a second implementer reading the README would.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def oracle_coerce(value, value_type: str):
    if value is None:
        raise ValueError("null in dictionary")
    if value_type == "string":
        assert isinstance(value, str)
        return value
    if value_type == "bool":
        assert isinstance(value, bool)
        return value
    if value_type == "int64":
        assert isinstance(value, int) and not isinstance(value, bool)
        return value
    if value_type == "double":
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        f = float(value)
        assert not math.isnan(f)
        return f
    raise ValueError(value_type)


@dataclass(frozen=True)
class OracleOutput:
    global_dictionary: tuple
    width: int
    # batch_id -> (local_to_global list, global_indices list, validity tuple)
    remaps: dict
    decoded_rows: dict  # batch_id -> tuple of scalar|None


def oracle_unify(raw_batches: list[dict], *, value_type: str,
                 index_policy: str = "auto", target_width=None,
                 max_cardinality: int = 2**32 - 1,
                 dedupe: bool = False) -> OracleOutput:
    """Reference implementation used by tests."""
    # 1. normalize each batch independently
    prepared = []  # (id, dict list, indices, validity)
    value_set: set = set()
    for rb in raw_batches:
        d = [oracle_coerce(v, value_type) for v in rb["dictionary"]]
        validity = (rb["validity"] if rb.get("validity") is not None
                    else [True] * len(rb["indices"]))

        # dedupe local dictionary (canonicalize to first code)
        first = {}
        canon = []
        cmap = []
        for code, v in enumerate(d):
            if v in first:
                if not dedupe:
                    raise AssertionError("duplicate local dictionary value")
                cmap.append(first[v])
            else:
                first[v] = len(canon)
                cmap.append(first[v])
                canon.append(v)
        d = canon
        indices = [cmap[i] for i in rb["indices"]] if dedupe else list(rb["indices"])

        prepared.append((rb["batch_id"], d, indices, validity))
        value_set.update(d)

    card = len(value_set)
    if card > max_cardinality:
        raise OverflowError("cardinality limit")

    gdict = sorted(value_set)
    gcode = {v: i for i, v in enumerate(gdict)}

    if index_policy == "strict":
        cap = {8: 255, 16: 65535, 32: 2**32 - 1}[target_width]
        if card > cap:
            raise OverflowError("strict width overflow")
        width = target_width
    else:
        width = 8 if card <= 255 else (16 if card <= 65535 else 32)

    remaps, decoded = {}, {}
    for bid, d, indices, validity in prepared:
        l2g = [gcode[v] for v in d]
        gidx, rows = [], []
        for i, valid in zip(indices, validity):
            if not valid:
                gidx.append(0)
                rows.append(None)
            else:
                code = l2g[i]
                gidx.append(code)
                rows.append(gdict[code])
        remaps[bid] = (l2g, gidx, tuple(validity))
        decoded[bid] = tuple(rows)

    return OracleOutput(global_dictionary=tuple(gdict), width=width,
                        remaps=remaps, decoded_rows=decoded)
