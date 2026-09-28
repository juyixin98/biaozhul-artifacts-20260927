"""流式与整段一致性、跨块令牌、转义跨块、尾部安全。"""
from __future__ import annotations

import itertools

import pytest

from app.core.redactor import StreamingRedactor
from tests.fixtures import (
    BANK_CARD,
    MOBILE,
    SAMPLE_LOG,
    SPLIT_POINTS,
    TOKEN,
    chunks_of,
)
from tests.oracle import oracle_redact


def _run_streaming(profile, text, sizes):
    """喂入流式数据；返回 (增量拼接, 完整结果)。

    增量拼接 = 每次 feed 的发出量 + finalize 返回的尾部增量；
    它应与 result.output（完整输出）逐字符相等。
    """
    red = StreamingRedactor(profile)
    emitted = []
    for piece in chunks_of(text, sizes):
        emitted.append(red.feed(piece).text)
    pre = "".join(emitted)
    result = red.finalize()
    tail = result.output[len(pre):]  # finalize 的尾部增量
    return pre + tail, result


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11, 13, 16, 17, 31, 32, 64])
def test_streaming_equals_whole_fixed_sizes(registry, size):
    profile = registry.get("standard")
    stream_out, result = _run_streaming(profile, SAMPLE_LOG, [size])
    whole = StreamingRedactor(profile)
    whole.feed(SAMPLE_LOG)
    whole_result = whole.finalize()

    assert result.status == "ok"
    assert stream_out == whole_result.output
    # 映射一致
    assert ([ (m.original_start, m.original_end, m.rule_id)
              for m in result.mappings ] ==
            [ (m.original_start, m.original_end, m.rule_id)
              for m in whole_result.mappings ])


def test_streaming_split_points_in_secret(registry):
    """每个切分点都恰好切开某个秘密时，仍必须完整脱敏。"""
    profile = registry.get("standard")
    sizes = [SPLIT_POINTS[i + 1] - SPLIT_POINTS[i]
             for i in range(len(SPLIT_POINTS) - 1)] + [37]
    stream_out, result = _run_streaming(profile, SAMPLE_LOG, sizes)
    assert result.status == "ok"
    whole = StreamingRedactor(profile)
    whole.feed(SAMPLE_LOG)
    assert stream_out == whole.finalize().output


def test_token_split_at_every_position(registry):
    """令牌在其 32 个位置中的任意一处被切成两半都不能泄漏。"""
    profile = registry.get("standard")
    text = f"lead {TOKEN} tail"
    for cut in range(1, len(text)):
        red = StreamingRedactor(profile)
        pre = red.feed(text[:cut]).text
        red.feed(text[cut:])
        result = red.finalize()
        # 任何中间发出不得包含原令牌
        assert TOKEN not in pre
        # 完整结果等于整段脱敏结果
        assert result.output == f"lead <TOKEN> tail"
        assert result.status == "ok"


def test_no_partial_value_emitted_early(registry):
    """未完整识别的尾片段不得提前放行：逐字符喂入时的每一次 emit。"""
    profile = registry.get("standard")
    red = StreamingRedactor(profile)
    seen = ""
    text = f"-{BANK_CARD}"  # '-' 是卡号正则认可的前导边界
    for ch in text:
        seen += red.feed(ch).text
    # finalize 之前：卡号任何数字都不应已出现在已发文本中
    digits_emitted = "".join(c for c in seen if c.isdigit())
    assert digits_emitted == "", f"提前泄露了卡号部分数字: {digits_emitted!r}"
    result = red.finalize()
    assert result.output == "-<BANK_CARD>"
    assert result.status == "ok"


def test_escape_split_across_chunks(registry):
    r'''JSON 转义序列 \" 与 \\ 被切在 chunk 边界上仍正确界定字段。'''
    profile = registry.get("standard")
    text = r'{"password":"ab\"cd"} tail'
    # 切在反斜杠与引号之间
    cut = text.index('\\"') + 1
    red = StreamingRedactor(profile)
    pre = red.feed(text[:cut]).text
    red.feed(text[cut:])
    result = red.finalize()
    combined = result.output
    assert result.status == "ok"
    assert 'ab\\"cd' not in combined
    assert '"password":<FIELD>' in combined.replace(" ", "")
    # 转义被正确理解为值内容：该字段被整体替换（含内部引号）
    assert combined.count("<FIELD>") == 1


def test_unicode_escape_split_across_chunks(registry):
    r"""中 四位 unicode 转义被切成多段时不失效。"""
    profile = registry.get("standard")
    text = r'{"password":"a中b"}'
    red = StreamingRedactor(profile)
    # 在 \ 与 u 之间、u 与 hex 之间、hex 中间分别切
    parts = [text[:14], text[14:16], text[16:18], text[18:]]
    pre = ""
    for p in parts:
        pre += red.feed(p).text
    result = red.finalize()
    assert result.status == "ok"
    assert "4e2d" not in pre  # 转义片段未提前流出
    assert result.output.replace(" ", "") == '{"password":<FIELD>}'


def test_streaming_emitted_spans_are_contiguous(registry):
    profile = registry.get("standard")
    red = StreamingRedactor(profile)
    pieces = chunks_of(SAMPLE_LOG, [13, 29, 7])
    spans = []
    for p in pieces:
        e = red.feed(p)
        spans.append((e.original_start, e.original_end))
    result = red.finalize()
    # 已发区间必须连续不重叠且单调
    for a, b in zip(spans, spans[1:]):
        assert a[1] == b[0]
    assert all(s[1] >= s[0] for s in spans)
    assert result.status == "ok"


def test_held_buffer_clears_after_finalize(registry):
    profile = registry.get("standard")
    red = StreamingRedactor(profile)
    red.feed("hello world no secrets here at all just plain prose")
    assert red.held_chars > 0  # 尾部 hold 窗口仍扣留
    result = red.finalize()
    assert result.status == "ok"
    assert red.held_chars == 0
