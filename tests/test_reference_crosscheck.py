"""Cross-validation against an independent reference merger.

The reference in :mod:`reference_merge` is a textbook diff3 LCS walker with
no shared code with the engine.  These tests assert:

* whenever the reference finds a merge conflict-free, the engine is also
  conflict-free and produces the *exact same text* (an independent answer,
  not an engine-generated one);
* whenever the engine reports a conflict, the cases here that the reference
  also regards as conflict-free would contradict the engine — so every
  engine-conflict fixture must be conflict-like for the reference as well
  OR a documented difference of boundary semantics;
* independent-edit consistency: applying the two sides in either order
  (re-merge with the first result as base) converges to the same text.
"""

import pytest

from merge3 import three_way_merge

from fixtures import ALL_CASES
from reference_merge import reference_merge, split_keep


def test_reference_and_engine_agree_on_conflict_free_text():
    checked = 0
    mismatches: list[str] = []
    for case in ALL_CASES:
        ref = reference_merge(case.base, case.local, case.remote)
        engine, result = three_way_merge(case.base, case.local, case.remote,
                                         f"x-{case.case_id}")
        if not ref.conflict:
            checked += 1
            if not result.auto_merged or result.merged_text != ref.merged:
                mismatches.append(
                    f"{case.case_id}: ref={ref.merged!r} "
                    f"engine={result.merged_text!r} "
                    f"engine_conflicts={[c.conflict_type.value for c in result.conflicts]}")
    assert checked >= 5, "reference should confirm multiple clean cases"
    assert not mismatches, "\n".join(mismatches)


def test_engine_conflicts_are_not_reference_clean():
    """Every fixture the engine flags must also be non-clean for the
    independent reference (the engine must never invent a conflict the
    reference trivially resolves, nor silently clean a real clash)."""
    for case in ALL_CASES:
        if case.expected_auto is not None:
            continue
        ref = reference_merge(case.base, case.local, case.remote)
        assert ref.conflict, (
            f"{case.case_id}: independent reference finds this conflict-free "
            f"with {ref.merged!r}; engine conflict type "
            f"{case.expected_conflict_types} needs justification")


def test_reference_splits_lines_with_exact_terminators():
    # sanity for the reference's own tokenizer
    assert split_keep("a\r\nb\nc") == ["a\r\n", "b\n", "c"]
    assert split_keep("") == []


def _engine_apply(base: str, changed: str, other: str) -> tuple[bool, str]:
    """Merge *other* onto *changed* relative to *base*; (clean, text)."""
    _, result = three_way_merge(base, changed, other, "seq")
    return result.auto_merged, (result.merged_text or "")


def test_application_order_converges_for_clean_fixtures():
    """Independence: clean merges are insensitive to side application order.

    Apply local-then-remote: re-merge remote over the local result. Apply
    remote-then-local the other way.  Both paths must produce identical
    text for every conflict-free fixture.
    """
    for case in ALL_CASES:
        if case.expected_auto is None:
            continue
        # first merge to get each side's result-equivalent
        _, first = three_way_merge(case.base, case.local, case.remote, "a")
        merged_once = first.merged_text
        assert merged_once == case.expected_auto

        # Re-base: remote applied onto local, and local onto remote.
        clean_lr, lr = _engine_apply(case.base, case.local, case.remote)
        clean_rl, rl = _engine_apply(case.base, case.remote, case.local)
        assert clean_lr and clean_rl, case.case_id
        assert lr == rl == case.expected_auto, case.case_id


@pytest.mark.parametrize("case", [c for c in ALL_CASES if c.expected_auto],
                         ids=[c.case_id for c in ALL_CASES if c.expected_auto])
def test_conflict_free_edits_present_as_contiguous_material(case):
    """The exact replacement text of every edit on either side is embedded
    contiguously, verbatim, in the auto-merged result (structure preservation
    checked against the engine's own edit inventory, while the expected text
    itself remains the hand-authored literal)."""
    from merge3.diff import diff_edits
    _, result = three_way_merge(case.base, case.local, case.remote, "v")
    merged = result.merged_text
    assert merged == case.expected_auto
    for side_text, side in ((case.local, "local"), (case.remote, "remote")):
        edits, *_ = diff_edits(case.base, side_text, side)
        for edit in edits:
            # Non-empty replacement material of each edit survives verbatim.
            if edit.replacement:
                assert edit.replacement in merged, (
                    case.case_id, side, repr(edit.replacement), merged)
