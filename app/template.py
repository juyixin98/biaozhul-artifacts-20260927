"""受限捕获模板。

语法（刻意做小，且不含任何可执行语义）
======================================
``\\g<name>``          按组名引用捕获
``\\1`` … ``\\99``     按十进制编号引用捕获（只吃数字，不支持前导 0 歧义写法）
``\\\\``                字面反斜杠
其余字符                原样字面量

不支持：``\\0``、``\\g<0>``、``\\g<1a>`` 之类非纯数字/非标识内容、反向引用
语义、条件/函数。任何无法解析的转义在 **计划前验证** 阶段直接拒绝。

两阶段
======
1. :func:`parse_template` 只解析不渲染，产出 token 序列并与规则捕获组清单
   比对——引用不存在的组，立刻报 :class:`InvalidTemplateError`。
2. :func:`render` 对具体命中渲染；严格模式下引用“未参与的可选组”报
   :class:`CaptureUnavailableError`，非严格模式渲染为空串。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import CaptureUnavailableError, InvalidTemplateError
from .engine import MatchHit

_LITERAL = "literal"
_REF_NAME = "name"
_REF_INDEX = "index"


@dataclass(frozen=True)
class TemplateToken:
    kind: str  # _LITERAL | _REF_NAME | _REF_INDEX
    value: str  # literal 文本 / 组名 / 组编号(字符串形式)
    pos: int    # 在模板中的起始位置，用于错误定位


@dataclass(frozen=True)
class ParsedTemplate:
    raw: str
    tokens: tuple[TemplateToken, ...]

    @property
    def references(self) -> tuple[TemplateToken, ...]:
        return tuple(t for t in self.tokens if t.kind != _LITERAL)


def parse_template(template: str, known_names: frozenset[str], group_count: int) -> ParsedTemplate:
    """解析并校验模板引用。

    :param known_names: 规则已定义的捕获组名集合（重复组名在引擎层已拒绝）
    :param group_count: 捕获组数量（编号 1..group_count 合法）
    """
    tokens: list[TemplateToken] = []
    buf: list[str] = []
    buf_start = 0
    i = 0
    n = len(template)

    def flush() -> None:
        if buf:
            tokens.append(TemplateToken(_LITERAL, "".join(buf), buf_start))
            buf.clear()

    while i < n:
        ch = template[i]
        if ch != "\\":
            if not buf:
                buf_start = i
            buf.append(ch)
            i += 1
            continue

        # 反斜杠转义
        if i + 1 >= n:
            raise InvalidTemplateError(
                "dangling backslash at end of template",
                details={"position": i},
            )
        nxt = template[i + 1]
        if nxt == "\\":
            if not buf:
                buf_start = i
            buf.append("\\")
            i += 2
        elif nxt == "g":
            # \g<...>
            if i + 2 >= n or template[i + 2] != "<":
                raise InvalidTemplateError(
                    r"expected '\g<...>' after backslash-g",
                    details={"position": i},
                )
            close = template.find(">", i + 3)
            if close == -1:
                raise InvalidTemplateError(
                    "unterminated \\g<...> reference",
                    details={"position": i},
                )
            inner = template[i + 3 : close]
            if not inner:
                raise InvalidTemplateError(
                    "empty capture reference \\g<>",
                    details={"position": i},
                )
            if inner[0].isdigit():
                if not inner.isdigit():
                    raise InvalidTemplateError(
                        "numeric \\g<...> reference must contain only digits",
                        details={"position": i, "value": inner},
                    )
                idx = int(inner)
                if idx < 1 or idx > group_count:
                    raise InvalidTemplateError(
                        "template references undefined capture group",
                        details={"position": i, "ref": inner, "group_count": group_count},
                    )
                flush()
                tokens.append(TemplateToken(_REF_INDEX, inner, i))
            else:
                # 名称：Python 标识符规则（引擎组名同样满足）
                if not (inner[0].isalpha() or inner[0] == "_") or not all(
                    c.isalnum() or c == "_" for c in inner
                ):
                    raise InvalidTemplateError(
                        "invalid capture group name in template",
                        details={"position": i, "value": inner},
                    )
                if inner not in known_names:
                    raise InvalidTemplateError(
                        "template references undefined named capture group",
                        details={"position": i, "name": inner},
                    )
                flush()
                tokens.append(TemplateToken(_REF_NAME, inner, i))
            i = close + 1
        elif nxt.isdigit():
            # \1 .. \99，贪婪吃掉连续数字但拒绝 \0
            j = i + 1
            while j < n and template[j].isdigit():
                j += 1
            digits = template[i + 1 : j]
            idx = int(digits)
            if idx < 1 or idx > group_count:
                raise InvalidTemplateError(
                    "template references undefined capture group",
                    details={"position": i, "ref": digits, "group_count": group_count},
                )
            flush()
            tokens.append(TemplateToken(_REF_INDEX, digits, i))
            i = j
        else:
            raise InvalidTemplateError(
                f"invalid escape in template: \\{nxt}",
                details={"position": i, "escape": nxt},
            )
    flush()
    return ParsedTemplate(raw=template, tokens=tuple(tokens))


def _resolve(token: TemplateToken, hit: MatchHit) -> tuple[bool, str | None]:
    """返回 (组是否参与, 文本)。未参与的可选组文本为 None。"""
    if token.kind == _REF_INDEX:
        idx = int(token.value)
        g = hit.groups[idx - 1]
        return g.text is not None, g.text
    # 按名称定位
    for g in hit.groups:
        if g.name == token.value:
            return g.text is not None, g.text
    # 解析阶段已验证存在，理论不可达
    raise InvalidTemplateError(
        "capture group disappeared at render time",
        details={"name": token.value},
    )


def render(parsed: ParsedTemplate, hit: MatchHit, *, strict_captures: bool = True) -> str:
    """对命中渲染替换文本。严格模式下可选组缺失会被拒绝。"""
    out: list[str] = []
    for token in parsed.tokens:
        if token.kind == _LITERAL:
            out.append(token.value)
            continue
        present, value = _resolve(token, hit)
        if not present:
            if strict_captures:
                raise CaptureUnavailableError(
                    "referenced capture group did not participate in this match",
                    details={
                        "ref": token.value,
                        "at_position": token.pos,
                        "char_span": [hit.start, hit.end],
                    },
                )
            value = ""
        out.append(value)
    return "".join(out)
