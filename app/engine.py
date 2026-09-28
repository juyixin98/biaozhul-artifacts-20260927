"""无回溯正则引擎封装（Google RE2）。

算法假设
========
* RE2 将正则编译成确定性自动机执行，**匹配时间对文本长度线性**，不存在
  stdlib ``re`` 那种指数级回溯；编译受 ``max_mem`` 预算约束。
* 引擎工作在 UTF-8 模式，匹配位置为 **码点偏移**（半开区间）。
* 不被引擎接受的特性（反向引用 ``\\1``、环视 ``(?=…)`` ``(?!…)`` ``(?<=…)``、
  命名反向引用 ``(?P=n)`` 等）在 **编译期** 以明确错误拒绝。
* 零宽前进规则（bump-along，与 Python 3.7+ ``re.finditer`` 一致）：

  1. 从游标 ``pos`` 开始寻找最左匹配；并列时引擎返回其标准最左优先匹配；
  2. 接受匹配后，若 ``end > start``（非零宽），下一轮游标 = ``end``；
     若 ``end == start``（零宽），下一轮游标 = ``start + 1``（串尾仍产出
     一次空匹配后结束）——**替换内容不重新进入同轮匹配**。
"""
from __future__ import annotations

from dataclasses import dataclass

import re2

from .config import LIMITS
from .errors import (
    InvalidFlagError,
    RegexCompileError,
    RegexProgramTooLargeError,
    ResourceExhaustedError,
)

# 受支持的标志名 -> Options 调整器。刻意只暴露语义明确、RE2 原生支持的子集。
_SUPPORTED_FLAGS = frozenset({"i", "s", "m"})
_FLAG_DOC = {
    "i": "大小写不敏感 (RE2 case_sensitive=False)",
    "s": "点号匹配换行 (RE2 dot_nl=True)",
    "m": "^/$ 按行锚定：RE2 绑定默认即多行语义（^/$ 识别 \\n）",
}


@dataclass(frozen=True)
class CapturedGroup:
    index: int
    name: str | None
    text: str | None  # None 表示该可选组本次未参与
    char_start: int
    char_end: int


@dataclass(frozen=True)
class MatchHit:
    """一条命中：码点范围 + 原始匹配文本 + 捕获组（整数索引 1..n）。"""

    start: int
    end: int
    text: str
    groups: tuple[CapturedGroup, ...]

    @property
    def is_zero_width(self) -> bool:
        return self.start == self.end


@dataclass(frozen=True)
class CompiledRulePattern:
    pattern_id: str
    regex: "re2._Regexp"  # type: ignore[name-defined]
    group_names: tuple[str | None, ...]  # 长度 = 组数+1，index 0 为整串(None)

    @property
    def group_count(self) -> int:
        return len(self.group_names) - 1


def _build_options(flags: frozenset[str]) -> "re2.Options":
    options = re2.Options()
    options.max_mem = LIMITS.regex_mem_budget
    # log_errors=False：解析错误通过异常路径返回，避免污染 stderr
    options.log_errors = False
    if "i" in flags:
        options.case_sensitive = False
    if "s" in flags:
        options.dot_nl = True
    # RE2 的 ^/$ 在 UTF8/perl 模式下默认即按 \n 行首行尾；"m" 接受为显式声明
    return options


def compile_pattern(
    pattern: str, flags: frozenset[str] | str = (), *, pattern_id: str = ""
) -> CompiledRulePattern:
    """编译规则正则，分类抛出编译错误。

    :raises InvalidFlagError: 标志名不在受支持子集
    :raises RegexCompileError: 语法错误或使用了无回溯引擎不支持的特性
    :raises RegexProgramTooLargeError: 程序超过 ``regex_mem_budget``
    """
    if isinstance(flags, str):
        flag_set = frozenset(flags)
    else:
        flag_set = frozenset(flags)
    unknown = sorted(flag_set - _SUPPORTED_FLAGS)
    if unknown:
        raise InvalidFlagError(
            f"unsupported regex flag(s): {''.join(unknown)}",
            details={"unknown": unknown, "supported": sorted(_SUPPORTED_FLAGS)},
        )
    options = _build_options(flag_set)
    try:
        regex = re2.compile(pattern, options)
    except Exception as exc:  # noqa: BLE001 - 绑定只抛单一 _re2.Error
        message = bytes(exc.args[0]).decode("utf-8", "replace") if exc.args else str(exc)
        if "too large" in message:
            raise RegexProgramTooLargeError(
                "regex program exceeded memory budget",
                details={"budget_bytes": LIMITS.regex_mem_budget, "reason": message},
            ) from exc
        raise RegexCompileError(
            "regex not accepted by the non-backtracking engine",
            details={"reason": message},
        ) from exc

    raw_index = dict(regex.groupindex)  # name -> index
    count = regex.groups
    names: list[str | None] = [None]
    seen: set[str] = set()
    for idx in range(1, count + 1):
        name = next((n for n, i in raw_index.items() if i == idx), None)
        if name is not None:
            if name in seen:
                raise RegexCompileError(
                    "duplicate capture group name",
                    details={"name": name},
                )
            seen.add(name)
        names.append(name)
    return CompiledRulePattern(pattern_id=pattern_id, regex=regex, group_names=tuple(names))


def scan_nonoverlapping(
    compiled: CompiledRulePattern,
    text: str,
    *,
    max_hits: int | None = None,
) -> list[MatchHit]:
    """显式 bump-along 扫描，产出同规则内互不重叠的有序命中。

    使用引擎 ``finditer``：它按本模块文档的零宽前进规则产出非重叠匹配，
    且比逐次 ``search`` 快一个数量级（绑定层生成器只建一次）。我们仍在此
    快照捕获组、计数并做预算判定，得到供多规则消解的确定命中列表。
    """
    hits: list[MatchHit] = []
    for m in compiled.regex.finditer(text):
        start, end = m.span()
        groups: list[CapturedGroup] = []
        for idx in range(1, compiled.group_count + 1):
            g_text = m.group(idx)
            gs, ge = m.span(idx)
            groups.append(
                CapturedGroup(
                    index=idx,
                    name=compiled.group_names[idx],
                    text=g_text,
                    char_start=gs,
                    char_end=ge,
                )
            )
        hits.append(MatchHit(start=start, end=end, text=m.group(0), groups=tuple(groups)))
        if max_hits is not None and len(hits) > max_hits:
            raise ResourceExhaustedError(
                "match count exceeded plan budget",
                details={"limit": max_hits, "pattern_id": compiled.pattern_id},
            )
    return hits
