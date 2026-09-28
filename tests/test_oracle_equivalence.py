"""Equivalence against the independent stdlib-re oracle.

These tests are the core correctness gate: the service's RE2-based result must
match a separately implemented reference (``tests/oracle.py``) on byte spans,
rule ownership and final text.  Expected answers are never produced by the
core package -- the oracle imports no app code.
"""

from __future__ import annotations

import random

import pytest

from app.planner import RuleSpec, apply_plan_stream, build_plan
from app.textspec import normalize_source
from tests.fixtures import samples
from tests.oracle import reference_plan, reference_apply


def specs(rule_dicts):
    return [
        RuleSpec(
            rule_id=r["rule_id"],
            pattern=r["pattern"],
            template=r["template"],
            priority=int(r.get("priority", 0)),
            flags=r.get("flags", ""),
            longest_match=r.get("longest_match", False),
            missing_capture=r.get("missing_capture", "error"),
        )
        for r in rule_dicts
    ]


def assert_equivalent(text, rule_dicts, logger=None, nodeid=None, tag=""):
    rules = specs(rule_dicts)
    data = text.encode("utf-8")
    result = build_plan(normalize_source(text).data, rules)

    ref_edits = reference_plan(text, rule_dicts)
    got_edits = result.plan.edits

    # 1) same number, spans (bytes), ownership and zero-width flags
    got_triples = [(e.start, e.end, e.rule_id, e.zero_width) for e in got_edits]
    ref_triples = [(e.byte_start, e.byte_end, e.rule_id, e.zero_width) for e in ref_edits]
    assert got_triples == ref_triples, f"{tag}\n got={got_triples}\n ref={ref_triples}"

    # 2) same final text, and it matches reference_apply
    out = apply_plan_stream(result.plan, data).output.decode("utf-8")
    ref_out = reference_apply(text, rule_dicts)
    assert out == ref_out, f"{tag}\n got={out!r}\n ref={ref_out!r}"

    if logger is not None:
        logger.check(
            nodeid,
            f"oracle equivalence {tag}",
            expected=ref_triples,
            actual=got_triples,
            passed=True,
            reason="RE2 byte spans/owners and stdlib re reference agree; "
                   "rendered output identical",
            intermediate={"text": text, "output": out, "rules": rule_dicts},
        )
    return out


def test_handcrafted_basic():
    for key, expected in samples.EXPECT_BASIC.items():
        out = assert_equivalent(samples.TEXTS[key], samples.RULESET_BASIC, tag=key)
        assert out == expected


def test_handcrafted_overlap_story(run_logger, request):
    out = assert_equivalent(
        samples.TEXTS["overlap_story"],
        samples.RULESET_OVERLAP,
        run_logger,
        request.node.nodeid,
        tag="overlap_story",
    )
    assert out == samples.EXPECT_OVERLAP


def test_handcrafted_lenient_optional():
    text = "Mr Smith; Ms; Doctor Who"
    out = assert_equivalent(text, samples.RULESET_OPTIONAL_LENIENT)
    assert out == samples.EXPECT_OPTIONAL_LENIENT


def test_zero_width_word_boundary_multibyte(run_logger, request):
    """Zero-width + non-empty rules over multibyte text.

    \b is ASCII-scoped in RE2 but Unicode-aware in stdlib re, so this case is
    NOT compared across engines. Instead it pins the service's documented
    interaction twice: (1) rule_id tie-break lets the boundary rule suppress a
    same-offset euro match; (2) giving euro higher priority restores the
    replacement while keeping adjacent boundary insertions.
    """
    from app.planner import apply_plan_stream as _apply

    text = samples.TEXTS["zero_width_play"]
    data = text.encode("utf-8")

    r1 = build_plan(data, specs(samples.RULESET_WORDBOUNDARY_EURO))
    out1 = _apply(r1.plan, data).output.decode("utf-8")
    assert out1 == samples.EXPECT_WORDBOUNDARY["zero_width_play"]
    # the € bytes were never consumed (euro was point-suppressed)
    assert "€" in out1

    r2 = build_plan(data, specs(samples.RULESET_EURO_PRIORITY))
    out2 = _apply(r2.plan, data).output.decode("utf-8")
    assert out2 == samples.EXPECT_EURO_PRIORITY
    assert "€" not in out2 and out2.count("EURO") == 2

    run_logger.check(
        request.node.nodeid,
        "zero-width multibyte priority interaction",
        expected=[samples.EXPECT_WORDBOUNDARY["zero_width_play"],
                  samples.EXPECT_EURO_PRIORITY],
        actual=[out1, out2],
        passed=[out1, out2] == [
            samples.EXPECT_WORDBOUNDARY["zero_width_play"],
            samples.EXPECT_EURO_PRIORITY,
        ],
        reason="same-offset zero-width/non-empty conflict resolved by rule_id "
               "then by declared priority; both plans UTF-8 aligned",
        intermediate={
            "edits_tiebreak": [(e.start, e.end, e.rule_id) for e in r1.plan.edits],
            "edits_priority": [(e.start, e.end, e.rule_id) for e in r2.plan.edits],
        },
    )


def test_zero_width_astar_chain_is_identical_to_stdlib():
    assert_equivalent("bab", [{"rule_id": "a", "pattern": r"a*", "template": "X"}])
    assert_equivalent("a€a", [{"rule_id": "a", "pattern": r"a*", "template": "X"}])


def test_adjacent_dense_matches():
    assert_equivalent("aaaa", [{"rule_id": "a", "pattern": r"a", "template": "AA"}])


def test_multiline_anchors():
    assert_equivalent(
        samples.TEXTS["lines"],
        [{"rule_id": "bol", "pattern": r"^.", "template": "($0)", "flags": "m"}],
    )


def test_multibyte_dense():
    text = samples.TEXTS["multibyte"]
    assert_equivalent(text, [
        {"rule_id": "euro", "pattern": r"€", "template": "EUR"},
        {"rule_id": "cjk", "pattern": r"[世界]", "template": "?"},
        {"rule_id": "b", "pattern": r"b", "template": "B", "priority": 5},
    ])


# --------------------------------------------------------------------------- #
# Deterministic randomized fuzz: RE2-safe patterns over multibyte alphabets,
# compared against stdlib re.  Fixed seeds -> replayable.
# --------------------------------------------------------------------------- #
ATOMS = ["a", "b", "c", "€", "世", "界", " ", "\n"]


def _rand_pattern(rng: random.Random) -> str:
    """Construct an RE2-safe pattern (also valid stdlib re)."""
    a = rng.choice(["a", "b", "c", "€", "世", " "])
    cls = "[" + "".join(rng.sample(["a", "b", "c", "€", "世"], rng.randrange(1, 3))) + "]"
    alts = "|".join(rng.sample(["a", "b", "€", "世"], rng.randrange(2, 4)))
    kinds = [
        a,
        f"{a}+",
        f"{a}*",
        f"{a}?",
        f"{cls}+",
        f"{cls}*",
        r"\b",
        f"(?:{alts})+",
        f"(?P<g>{rng.choice(['a', 'b', '€', '世'])}+)",
        f"({rng.choice(['a', 'b', '€'])})({rng.choice(['b', 'c', '界'])})?",
        r"ab",
    ]
    return rng.choice(kinds)


def _safe_pattern_and_template(rng: random.Random):
    pat = _rand_pattern(rng)
    import re as _re

    try:
        compiled = _re.compile(pat)
    except _re.error:
        return None
    # Fuzz covers the always-participating capture surface; optional/unmatched
    # groups (the (...)? variant) and their strict-vs-lenient policy are pinned
    # by dedicated hand-written tests, so skip them here.
    if pat.endswith(")?"):
        return None
    ng = compiled.groups
    choices = ["X", "$0", "[$0]", ""]
    if ng >= 1:
        choices += ["$1"]
        if "g" in compiled.groupindex:
            choices += ["${g}"]
    tpl = rng.choice(choices)
    return pat, tpl, "error"


def _rand_text(rng: random.Random) -> str:
    n = rng.randrange(0, 24)
    return "".join(rng.choice(ATOMS) for _ in range(n))


def _rand_ascii_text(rng: random.Random) -> str:
    alphabet = ["a", "b", "c", " "]
    n = rng.randrange(0, 24)
    return "".join(rng.choice(alphabet) for _ in range(n))


def _is_ascii_only_pattern(pat: str) -> bool:
    # Zero-width \b is ASCII-word-boundary semantics in RE2 but Unicode-aware
    # in stdlib re; compare it only on ASCII documents.
    return pat == r"\b" or all(ord(ch) < 128 for ch in pat)


@pytest.mark.parametrize("seed", list(range(60)))
def test_random_equivalence_single_rule(seed):
    rng = random.Random(1000 + seed)
    for _ in range(5):
        pt = _safe_pattern_and_template(rng)
        if pt is None:
            continue
        pat, tpl, _policy = pt
        # \b has different word-char alphabets across engines (ASCII vs
        # Unicode); only compare it on ASCII documents.
        text = _rand_ascii_text(rng) if pat == r"\b" else _rand_text(rng)
        assert_equivalent(text, [{"rule_id": "r", "pattern": pat, "template": tpl}])


@pytest.mark.parametrize("seed", list(range(30)))
def test_random_equivalence_multi_rule(seed, run_logger, request):
    rng = random.Random(2000 + seed)
    rule_dicts = []
    used_ids = set()
    for k in range(rng.randrange(2, 5)):
        pt = _safe_pattern_and_template(rng)
        if pt is None:
            continue
        pat, tpl, _policy = pt
        rid = f"r{k}"
        if rid in used_ids:
            continue
        used_ids.add(rid)
        rule_dicts.append({
            "rule_id": rid,
            "pattern": pat,
            "template": tpl,
            "priority": rng.randrange(0, 4),
        })
    if len(rule_dicts) < 2:
        pytest.skip("fuzz draw produced too few usable rules")
    # If any rule relies on \b, keep the document ASCII so RE2's ASCII word
    # boundaries and stdlib's Unicode ones coincide.
    if any(rd["pattern"] == r"\b" for rd in rule_dicts):
        text = _rand_ascii_text(rng)
    else:
        text = _rand_text(rng)
    assert_equivalent(
        text, rule_dicts, run_logger, request.node.nodeid, tag=f"seed{seed}"
    )
