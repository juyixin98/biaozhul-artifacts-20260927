"""全局可调限制与运行编号。

所有限制均为服务端硬限制：超出后以明确的错误类别拒绝，而不是静默截断。
环境变量便于在资源受限机器上缩小预算做“资源耗尽”测试。
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Limits:
    max_text_chars: int = _int_env("NRP_MAX_TEXT_CHARS", 5_000_000)
    # 单条规则的正则程序内存预算（RE2 max_mem, 字节）
    regex_mem_budget: int = _int_env("NRP_REGEX_MEM", 1 << 20)  # 1 MiB
    max_pattern_chars: int = _int_env("NRP_MAX_PATTERN_CHARS", 4_096)
    max_template_chars: int = _int_env("NRP_MAX_TEMPLATE_CHARS", 4_096)
    max_rules_per_plan: int = _int_env("NRP_MAX_RULES", 200)
    max_rule_id_chars: int = 128
    # 一次计划允许的替换条目上限（防止规则在超大文本上产生海量零宽匹配拖垮存储）
    max_matches_per_plan: int = _int_env("NRP_MAX_MATCHES", 200_000)
    # 应用阶段每个流式分片的码点数
    apply_chunk_chars: int = _int_env("NRP_CHUNK_CHARS", 65_536)


LIMITS = Limits()


def new_run_id() -> str:
    """生成可重放定位的运行编号：时间序 + 随机短段。"""
    # UUID1 含时间戳与节点；取 hex 足够唯一且可按前缀排序定位
    return "run-" + uuid.uuid1().hex
