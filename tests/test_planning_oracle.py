"""规划核心测试：与独立参考实现 tests/oracle.py 全面对账。

场景覆盖需求点名的五类：零宽、相邻匹配、捕获缺失、多规则重叠、多字节文本。
断言具体的码点/字节位置、替换结果与淘汰原因，而非“能跑通”。
"""
from __future__ import annotations

import pytest

from app import planning
from app.schemas import RuleIn
from app.textutil import ByteOffsetMap

from . import fixtures, oracle


def _rules(specs):
    return [
        RuleIn(
            rule_id=s["rule_id"],
            pattern=s["pattern"],
            template=s["template"],
            flags=s.get("flags", ""),
            priority=s.get("priority", 100),
            strict_captures=s.get("strict_captures", True),
        )
        for s in specs
    ]


def _core_spans(plan):
    return [
        (c.hit.start, c.hit.end, c.rule.rule_id, c.replacement, c.hit.is_zero_width)
        for c in plan.chosen
    ]


def _oracle_spans(chosen):
    return [(c.hit.start, c.hit.end, c.hit.rule_id, c.replacement, c.hit.zw) for c in chosen]


def _core_displaced(plan):
    return [(d.rule_id, d.char_start, d.char_end, d.reason) for d in plan.displaced]


def _oracle_displaced(displaced):
    return [(h.rule_id, h.start, h.end, why) for h, why in displaced]


@pytest.mark.parametrize(
    "text,specs",
    [
        # 1) 纯零宽：在每个码点边界插入
        ("ab", [{"rule_id": "ins", "pattern": "", "template": "|"}]),
        # 2) 相邻匹配 cat|dog
        (fixtures.adjacency_text(), [
            {"rule_id": "pet", "pattern": "cat|dog", "template": "X"},
        ]),
        # 3) 零宽与消耗混合（a* 自身就含零宽）
        ("aab", [{"rule_id": "a", "pattern": "a*", "template": "("}]),
        # 5) 多字节
        (fixtures.multibyte_text(), [
            {"rule_id": "code", "pattern": r"编号([A-C])-(\d)", "template": r"[\1#\2]"},
            {"rule_id": "ins", "pattern": "", "template": "·", "priority": 200},
        ]),
        # 邮箱捕获，多字节缺失
        (fixtures.capture_text(), [
            {"rule_id": "mail", "pattern": r"(?P<u>\w+)@(?P<h>\w+)", "template": r"<\g<u>@\2>"},
        ]),
    ],
)
def test_core_matches_oracle_span_and_output(record, text, specs):
    rules = _rules(specs)
    plan = planning.build_plan(text, rules)
    o_chosen, o_disp = oracle.o_plan(text, specs)
    core_spans, ora_spans = _core_spans(plan), _oracle_spans(o_chosen)
    record.state("core_spans", core_spans)
    record.state("oracle_spans", ora_spans)
    record.check(
        "chosen spans identical to independent oracle",
        ok=core_spans == ora_spans,
        expected=ora_spans,
        actual=core_spans,
    )
    assert core_spans == ora_spans

    core_disp, ora_disp = _core_displaced(plan), _oracle_displaced(o_disp)
    record.check("displaced identical", ok=core_disp == ora_disp, expected=ora_disp, actual=core_disp)
    assert core_disp == ora_disp

    # 应用结果与 oracle 独立拼接对账
    from app.apply import apply_plan
    core_out = apply_plan(text, plan, source_version=1, expected_version=1)
    ora_out = oracle.o_apply(text, specs)
    record.check("applied text identical", ok=core_out == ora_out, expected=ora_out, actual=core_out)
    assert core_out == ora_out


def test_zero_width_exact_output_handwritten(record):
    # 手写常量，双重独立：空模式插入 "-" 于 "ab" => -a-b-
    plan = planning.build_plan("ab", _rules([{"rule_id": "z", "pattern": "", "template": "-"}]))
    from app.apply import apply_plan
    out = apply_plan("ab", plan, source_version=1, expected_version=1)
    record.state("output", out)
    assert out == "-a-b-"
    assert plan.zero_width_count == 3


def test_adjacent_consuming_matches_not_over_accepted(record):
    specs = [{"rule_id": "pet", "pattern": "cat|dog", "template": "PET"}]
    plan = planning.build_plan("catdog", _rules(specs))
    spans = [(c.hit.start, c.hit.end) for c in plan.chosen]
    record.state("spans", spans, "相邻两个消耗型都应入选")
    assert spans == [(0, 3), (3, 6)]
    assert plan.displaced == []


def test_multi_rule_overlap_priority_resolution(record):
    # aaaa 上两条规则各自做 bump-along 非重叠扫描：
    #   high(aa)：[0,2)、[2,4)
    #   low(aaa)：[0,3)；下一轮从 3 起，剩 "a" 不足，故只产生一条
    # low[0,3) 与 high[0,2) 同起点，被高优先级覆盖（消解不重扫）。
    specs = [
        {"rule_id": "high", "pattern": "aa", "template": "H", "priority": 10},
        {"rule_id": "low", "pattern": "aaa", "template": "L", "priority": 50},
    ]
    plan = planning.build_plan("aaaa", _rules(specs))
    chosen = [(c.rule.rule_id, c.hit.start, c.hit.end) for c in plan.chosen]
    displaced = [(d.rule_id, d.char_start, d.char_end, d.reason) for d in plan.displaced]
    record.state("chosen", chosen)
    record.state("displaced", displaced)
    assert chosen == [("high", 0, 2), ("high", 2, 4)]
    assert displaced == [("low", 0, 3, "covered_by_higher_priority")]


def test_multi_rule_overlap_crossing_priority(record):
    # 交叉重叠：long 跨 [0,4) 高优先级；short 在其内部 [1,3) 低优先级 -> 被覆盖
    specs = [
        {"rule_id": "long", "pattern": "a..a", "template": "L", "priority": 1},
        {"rule_id": "short", "pattern": r".\w.", "template": "S", "priority": 99},
    ]
    plan = planning.build_plan("abca", _rules(specs))
    chosen = [(c.rule.rule_id, c.hit.start, c.hit.end) for c in plan.chosen]
    record.state("chosen", chosen)
    assert ("long", 0, 4) in chosen
    assert all(c.rule.rule_id != "short" for c in plan.chosen)
    assert plan.displaced[0].reason == "covered_by_higher_priority"


def test_same_priority_declaration_order_wins(record):
    # 同起点同优先级：声明在前者胜出
    specs = [
        {"rule_id": "first", "pattern": "abc", "template": "1"},
        {"rule_id": "second", "pattern": "abc", "template": "2"},
    ]
    plan = planning.build_plan("abc", _rules(specs))
    assert [(c.rule.rule_id, c.replacement) for c in plan.chosen] == [("first", "1")]
    # 同点同优先级完全并列，理由归入“同优先级先声明者”；二者的区分本质是声明序
    assert plan.displaced[0].reason == "covered_by_earlier_same_priority"


def test_consuming_beats_zero_width_at_same_start(record):
    specs = [
        {"rule_id": "zw", "pattern": "", "template": "0", "priority": 1},
        {"rule_id": "eat", "pattern": "ab", "template": "X", "priority": 99},
    ]
    plan = planning.build_plan("ab", _rules(specs))
    first = (plan.chosen[0].rule.rule_id, plan.chosen[0].hit.start, plan.chosen[0].hit.end)
    record.state("first_action", first, "同点消耗型必须先于零宽")
    assert first == ("eat", 0, 2)
    # 零宽 [0,0) 与消耗 [0,2) 同点：零宽落在消耗起点（边界），仍可保留
    rule_ids = {(c.rule.rule_id, c.hit.start, c.hit.end) for c in plan.chosen}
    assert ("zw", 0, 0) in rule_ids


def test_strict_capture_missing_fails_planning(record):
    # carol@缺失：\w+ 不匹配中文，主机组可能缺失 —— 用可选组精确构造
    specs = [
        {"rule_id": "opt", "pattern": r"(?P<a>\d+)?(\w+)", "template": r"\1-\2",
         "strict_captures": True},
    ]
    from app.errors import CaptureUnavailableError
    with pytest.raises(CaptureUnavailableError) as ei:
        planning.build_plan("abc", _rules(specs))
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "COMPUTE_CAPTURE_UNAVAILABLE"


def test_lenient_capture_missing_renders_empty_and_matches_oracle(record):
    specs = [
        {"rule_id": "opt", "pattern": r"(?P<a>\d+)?(\w+)", "template": r"[\1][\2]",
         "strict_captures": False},
    ]
    plan = planning.build_plan("ab9", _rules(specs))
    from app.apply import apply_plan
    out = apply_plan("ab9", plan, source_version=1, expected_version=1)
    ora = oracle.o_apply("ab9", specs)
    record.check("lenient output vs oracle", ok=out == ora, expected=ora, actual=out)
    assert out == ora
    record.state("output", out)
    # 手写常量：\w+ 在 a/b 处整体吃成一个词（含缺失数字组），9 单独成词
    assert out == "[][ab9]"


def test_byte_ranges_verified_for_multibyte_matches(record):
    text = fixtures.multibyte_text()
    specs = [{"rule_id": "code", "pattern": r"编号([A-C])-(\d)", "template": r"{\1}"}]
    plan = planning.build_plan(text, _rules(specs))
    bm = ByteOffsetMap(text)
    encoded = text.encode("utf-8")
    for c in plan.chosen:
        # 计划内字节范围必须切回原匹配文本
        sliced = encoded[c.byte_start:c.byte_end].decode("utf-8")
        record.check(
            f"byte slice for {c.rule.rule_id}@{c.hit.start}",
            ok=sliced == c.hit.text,
            expected=c.hit.text,
            actual=sliced,
        )
        assert sliced == c.hit.text
        # 且与独立换算一致
        assert (c.byte_start, c.byte_end) == bm.char_span_to_byte_span(c.hit.start, c.hit.end)


def test_replacement_does_not_reenter_matching(record):
    # 替换文本本身包含会被规则匹配的内容，但同轮不允许二次匹配
    specs = [{"rule_id": "x", "pattern": "x", "template": "xx"}]
    plan = planning.build_plan("x", _rules(specs))
    assert len(plan.chosen) == 1
    from app.apply import apply_plan
    out = apply_plan("x", plan, source_version=1, expected_version=1)
    record.state("output", out, "替换出的 xx 不得在同轮再次触发规则")
    assert out == "xx"  # 若重新进入匹配将无限增长（规划期只产出 1 条）


def test_duplicate_rule_id_rejected():
    from app.errors import InvalidRuleError
    specs = [
        {"rule_id": "r", "pattern": "a", "template": "1"},
        {"rule_id": "r", "pattern": "b", "template": "2"},
    ]
    with pytest.raises(InvalidRuleError) as ei:
        planning.build_plan("ab", _rules(specs))
    assert ei.value.code == "INPUT_INVALID_RULE"


def test_random_large_text_oracle_consistency(record):
    # oracle 是朴素 O(n²) 参考实现，对账规模取 8k（覆盖多块锚点与大量零宽）；
    # 120k 级的核心性能/自洽在 test_large_text_performance 单独保证。
    text = fixtures.large_multibyte_text(8_000)
    specs = [
        {"rule_id": "mail", "pattern": r"(\w+)@(\w+)\.(\w+)", "template": r"[\1@\2.\3]",
         "priority": 20},
        {"rule_id": "han", "pattern": r"你好", "template": "HH", "priority": 10},
        {"rule_id": "ins", "pattern": "", "template": "", "priority": 200},
        {"rule_id": "digit", "pattern": r"\d+", "template": "#", "priority": 5},
    ]
    plan = planning.build_plan(text, _rules(specs))
    o_chosen, o_disp = oracle.o_plan(text, specs)
    core_spans = _core_spans(plan)
    record.state("chosen_count", len(core_spans))
    record.state("displaced_count", len(plan.displaced))
    assert core_spans == _oracle_spans(o_chosen)
    assert _core_displaced(plan) == _oracle_displaced(o_disp)
    from app.apply import apply_plan
    assert apply_plan(text, plan, source_version=1, expected_version=1) == oracle.o_apply(text, specs)


def test_large_text_performance_and_self_consistency(record):
    # 12 万码点多字节文本：核心规划应在线性到近线性时间完成，且
    # 计划条目满足“互不重叠、排序确定、字节范围可往返”的自洽不变式。
    text = fixtures.large_multibyte_text(120_000)
    specs = [
        {"rule_id": "mail", "pattern": r"(\w+)@(\w+)\.(\w+)", "template": r"[\1@\2.\3]",
         "priority": 20},
        {"rule_id": "han", "pattern": r"你好", "template": "HH", "priority": 10},
        {"rule_id": "ins", "pattern": "", "template": "·", "priority": 200},
        {"rule_id": "digit", "pattern": r"\d+", "template": "#", "priority": 5},
    ]
    import time
    start = time.time()
    plan = planning.build_plan(text, _rules(specs))
    elapsed = time.time() - start
    record.state("elapsed_seconds", round(elapsed, 3), "120k 多字节规划应在数秒内")
    assert elapsed < 10.0

    # 不变式 1：严格按排序键有序
    keys = [planning.Candidate.sort_key(c) for c in plan.chosen]
    assert keys == sorted(keys)
    # 不变式 2：任意两条不相交（零宽同点只允许一条）
    spans = [(c.hit.start, c.hit.end) for c in plan.chosen]
    zero_seen: set[int] = set()
    last_end = -1
    for s, e in spans:
        if s == e:
            assert s not in zero_seen
            zero_seen.add(s)
        else:
            assert s >= last_end or True  # 消耗区间之间不重叠（下面集合式校验）
            last_end = max(last_end, e)
    # 集合式：消耗型两两不相交
    consuming = sorted((s, e) for s, e in spans if e > s)
    for (s1, e1), (s2, e2) in zip(consuming, consuming[1:]):
        assert e1 <= s2, f"overlapping consuming entries [{s1},{e1}) [{s2},{e2})"
    # 不变式 3：字节范围往返
    encoded = text.encode("utf-8")
    for c in plan.chosen:
        assert encoded[c.byte_start:c.byte_end].decode("utf-8") == c.hit.text
    record.state("chosen", len(plan.chosen), "displaced", )
    record.state("displaced", len(plan.displaced))
    assert len(plan.chosen) > 100_000  # 空模式逐码点贡献大量零宽动作
