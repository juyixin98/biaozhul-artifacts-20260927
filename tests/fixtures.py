"""测试专用的本地合成夹具。

不依赖任何外部服务或真实业务数据；全部文本在进程内构造，覆盖多字节、
零宽、相邻匹配与大文本压力场景。
"""
from __future__ import annotations

import os


def multibyte_text() -> str:
    """多字节 + ASCII 混合，含多个可命中点，边界清晰。"""
    return "编号A-1：你好，世界！编号B-2 测试 αβγ 编号C-3 end"


def adjacency_text() -> str:
    """相邻/紧挨着的匹配：cat 后紧跟 dog，以及零宽点。"""
    return "catdogcat!!dog"


def zero_width_text() -> str:
    return "ab\ncd"


def overlap_rules_text() -> str:
    return "aaaa"


def capture_text() -> str:
    return "alice@example.com bob(eng) carol@缺失 2026-09-28"


def large_multibyte_text(chars: int | None = None) -> str:
    """确定性伪随机大文本（固定种子，保证可重放），多字节占比稳定。"""
    chars = chars or int(os.environ.get("NRP_TEST_LARGE_CHARS", "120_000"))
    alphabet = "abc01 \n你好世界αβγ-@."
    out = []
    state = 0x1234_ABCD
    mask = (1 << 32) - 1
    for _ in range(chars):
        # xorshift32
        state ^= (state << 13) & mask
        state ^= state >> 17
        state ^= (state << 5) & mask
        state &= mask
        out.append(alphabet[state % len(alphabet)])
    # 埋入若干确定性可命中锚点
    s = "".join(out)
    anchors = ["alpha@example.com", "beta@example.org", "gamma@test.net"]
    parts = [s]
    for k, anchor in enumerate(anchors):
        at = (k + 1) * len(s) // (len(anchors) + 1)
        parts.append((at, anchor))
    for at, anchor in sorted(parts[1:], reverse=True):
        s = s[:at] + anchor + s[at:]
    return s


def invalid_utf8_bytes() -> bytes:
    return b"ok\xff\xfe-bad-tail\xc3\x28"
