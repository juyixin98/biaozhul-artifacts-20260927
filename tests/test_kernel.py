"""Kernel-level tests: concrete outputs, not merely callable endpoints."""
from __future__ import annotations

import json

import pytest

from app.kernel import (
    StreamRedactor,
    redact_full,
    resolve_overlaps,
    scan_uncertain,
    Span,
)
from app.rules import Rule, RuleKind, build_ruleset, luhn_ok
from tests.synth_fixtures import (
    BAD_BANK_CARD,
    BAD_CN_ID,
    SYNTH_API_TOKEN,
    SYNTH_BANK_CARD,
    SYNTH_CN_ID,
    SYNTH_EMAIL,
    SYNTH_MOBILE,
    SYNTH_SECRET_HEX,
    TRUNCATED_TOKEN,
)

STANDARD = build_ruleset("standard")
STRICT = build_ruleset("strict")


# ---------------------------------------------------------------------------
# Exact-result redaction
# ---------------------------------------------------------------------------


def test_email_redacted_to_exact_token():
    result = redact_full(f"contact {SYNTH_EMAIL} today", STANDARD)
    assert result.redacted == "contact [REDACTED:email] today"
    assert len(result.mappings) == 1
    m = result.mappings[0]
    assert m.rule_id == "std.pattern.email"
    assert (m.original_start, m.original_end) == (8, 8 + len(SYNTH_EMAIL))
    assert (m.output_start, m.output_end) == (8, 8 + len("[REDACTED:email]"))
    assert m.original_length == len(SYNTH_EMAIL)
    assert not m.uncertain


def test_no_partial_original_remains():
    """No prefix/suffix of the token (>=6 chars) may survive in output."""
    text = f"key={SYNTH_API_TOKEN};"
    result = redact_full(text, STANDARD)
    assert SYNTH_API_TOKEN not in result.redacted
    for n in range(6, len(SYNTH_API_TOKEN)):
        assert SYNTH_API_TOKEN[:n] not in result.redacted
        assert SYNTH_API_TOKEN[-n:] not in result.redacted
    assert result.redacted == "key=[REDACTED:api_token];"


def test_repeated_fragments_each_mapped():
    text = f"{SYNTH_EMAIL} {SYNTH_EMAIL}"
    result = redact_full(text, STANDARD)
    assert result.redacted == "[REDACTED:email] [REDACTED:email]"
    assert len(result.mappings) == 2
    assert result.mappings[0].original_end <= result.mappings[1].original_start
    # Output coords account for length change.
    assert result.mappings[0].output_end == len("[REDACTED:email]")
    assert result.mappings[1].output_start == len("[REDACTED:email] ")


def test_adjacent_rules_both_redacted():
    """Adjacent (non-overlapping) secrets must both be replaced."""
    text = f"{SYNTH_EMAIL}{SYNTH_MOBILE}"
    result = redact_full(text, STANDARD)
    assert result.redacted == "[REDACTED:email][REDACTED:cn_mobile]"
    assert {m.label for m in result.mappings} == {"email", "cn_mobile"}


def test_overlap_field_beats_inner_pattern():
    """An api token inside a structured token field: one replacement only."""
    text = f'"token":"{SYNTH_API_TOKEN}"'
    result = redact_full(text, STANDARD)
    assert result.redacted == '"token":"[REDACTED:field]"'
    assert len(result.mappings) == 1
    assert result.mappings[0].label == "field"
    assert SYNTH_API_TOKEN not in result.redacted


def test_bank_card_requires_luhn_evidence():
    good = f"pan {SYNTH_BANK_CARD} ok"
    r_good = redact_full(good, STANDARD)
    assert luhn_ok(SYNTH_BANK_CARD)
    assert r_good.redacted == "pan [REDACTED:bank_card] ok"

    bad = f"pan {BAD_BANK_CARD} x"
    r_bad = redact_full(bad, STANDARD)
    # The invalid-checksum value is NOT confidently redacted...
    assert r_bad.mappings == []
    # ...but it is surfaced separately as an uncertain finding.
    assert len(r_bad.uncertain) == 1
    u = r_bad.uncertain[0]
    assert u.label == "bank_card?"
    assert u.reason == "evidence_check_failed:luhn"
    assert u.original == BAD_BANK_CARD


def test_cn_id_only_in_strict_profile_and_validated():
    r_std = redact_full(SYNTH_CN_ID, STANDARD)
    assert r_std.redacted == SYNTH_CN_ID  # rule absent in standard
    r_strict = redact_full(SYNTH_CN_ID, STRICT)
    assert r_strict.redacted == "[REDACTED:cn_id_card]"
    r_bad = redact_full(BAD_CN_ID, STRICT)
    assert r_bad.mappings == []
    assert any(u.reason == "evidence_check_failed:cn_id_checksum" for u in r_bad.uncertain)


# ---------------------------------------------------------------------------
# Escaped JSON: redaction must leave parseable structure
# ---------------------------------------------------------------------------


def test_escaped_json_field_redaction_preserves_json():
    inner = json.dumps(
        {"user": "bob", "email": SYNTH_EMAIL, "token": SYNTH_API_TOKEN},
        separators=(",", ":"),
    )
    envelope = json.dumps({"payload": inner})  # inner quotes now escaped
    result = redact_full(envelope, STANDARD)
    parsed = json.loads(result.redacted)
    restored = json.loads(parsed["payload"])
    assert restored["email"] == "[REDACTED:field]"
    assert restored["token"] == "[REDACTED:field]"
    assert SYNTH_EMAIL not in result.redacted
    assert SYNTH_API_TOKEN not in result.redacted


def test_bare_field_form_redacted():
    result = redact_full("password=hunter2hunter done", STANDARD)
    assert result.redacted == "password=[REDACTED:field] done"


# ---------------------------------------------------------------------------
# Streaming: tail never released early; stream == whole-document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cut", list(range(1, 20)))
def test_streaming_equals_full_for_every_cut(cut):
    text = (
        f"start {SYNTH_EMAIL} middle {SYNTH_API_TOKEN} "
        f"{SYNTH_MOBILE} x{SYNTH_BANK_CARD}y end "
    )
    full = redact_full(text, STANDARD)

    sr = StreamRedactor(STANDARD)
    pieces = [sr.push(text[i : i + cut]) for i in range(0, len(text), cut)]
    pieces.append(sr.finish())
    streamed = "".join(pieces)

    assert streamed == full.redacted
    assert [
        (m.rule_id, m.original_start, m.original_end) for m in sr.mappings
    ] == [
        (m.rule_id, m.original_start, m.original_end) for m in full.mappings
    ]
    assert sr.input_length == len(text)
    assert sr._out_pos == len(full.redacted)


def test_token_split_across_chunks_never_partially_emitted():
    """Before the token is complete no output may expose a prefix of it."""
    sr2 = StreamRedactor(STANDARD)
    emitted = ""
    for ch in "log line " + SYNTH_API_TOKEN:
        emitted += sr2.push(ch)
    emitted += sr2.finish()
    assert emitted == "log line [REDACTED:api_token]"
    # At no point during arrival could the assembled output contain the
    # first 6 characters of the secret adjacent to later characters.
    assert SYNTH_API_TOKEN[:6] not in emitted


def test_unfinished_stream_does_not_release_tail():
    """Without finish(), the trailing window must stay buffered (not leaked)."""
    sr = StreamRedactor(STANDARD)
    out = sr.push("x" * 10 + " " + SYNTH_API_TOKEN)  # token sits at the tail
    assert SYNTH_API_TOKEN[:6] not in out
    assert SYNTH_API_TOKEN not in out
    rest = sr.finish()
    assert rest.endswith("[REDACTED:api_token]")


def test_streaming_uncertainties_match_full():
    text = f"a {BAD_BANK_CARD} b {TRUNCATED_TOKEN} c"
    full = redact_full(text, STANDARD)
    sr = StreamRedactor(STANDARD)
    out = ""
    for i in range(0, len(text), 7):
        out += sr.push(text[i : i + 7])
    out += sr.finish()
    assert out == full.redacted == text  # nothing confidently redacted
    assert {u.rule_id for u in sr.uncertain} == {
        u.rule_id for u in full.uncertain
    }
    assert any(u.reason == "truncated_token_prefix" for u in sr.uncertain)


# ---------------------------------------------------------------------------
# Deterministic overlap arbitration
# ---------------------------------------------------------------------------


def test_resolve_overlaps_priority_and_adjacency():
    def span(rid, prio, s, e):
        return Span(s, e, rid, rid, prio, "x" * (e - s), "R")

    a = span("low", 10, 0, 5)
    b = span("high", 20, 3, 8)
    assert [s.rule_id for s in resolve_overlaps([a, b])] == ["high"]
    # Adjacent spans coexist.
    c = span("x", 10, 0, 5)
    d = span("y", 10, 5, 9)
    assert {s.rule_id for s in resolve_overlaps([c, d])} == {"x", "y"}


def test_unknown_profile_and_bad_regex_are_named_failures():
    with pytest.raises(Exception) as ei:
        build_ruleset("nope")
    assert ei.value.__class__.__name__ == "UnknownProfileError"
    with pytest.raises(Exception) as ei2:
        Rule(
            rule_id="bad",
            kind=RuleKind.PATTERN,
            label="bad",
            pattern="(",
            priority=1,
            max_len=4,
        )
    assert ei2.value.__class__.__name__ == "RuleCompileError"


def test_secret_hex_redacted():
    r = redact_full(SYNTH_SECRET_HEX, STANDARD)
    assert r.redacted == "[REDACTED:secret]"
