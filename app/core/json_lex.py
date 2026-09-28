"""增量 JSON 结构词法扫描器。

职责（只负责"证据"，不负责替换）：
- 在流式到达的文本中识别形如 ``"敏感键": "值"`` / ``"敏感键": 123`` 的
  字段区间，区间包含字符串两侧引号，供上层整体替换；
- 正确处理 JSON 转义（``\\"``、``\\\\``、``\\uXXXX`` 等），转义序列可以
  落在 chunk 边界上，跨块续扫状态不丢失；
- 任何无法确定的情形（非法转义、结构损坏、字符串到 EOF 未闭合、
  值长度超限）都产出 ``uncertainty`` 事件并停用字段识别——绝不猜测性
  放行；
- 非 JSON 普通日志不受影响：字段识别仅在见到 ``{`` / ``[`` 结构后激活，
  顶层散文按透传处理。

输出位置均为相对整个输入的绝对字符偏移。safe_pos 表示"到此位置之前的
字段判定已稳定、可安全发出"的边界；敏感值未闭合时 safe_pos 停在值起点，
保证敏感片段的任何部分都不会被提前放行。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

UncertaintyCode = Literal[
    "INVALID_ESCAPE",
    "STRUCTURAL_INVALID",
    "UNCLOSED_STRING",
    "OVERLONG_FIELD_VALUE",
]


@dataclass(frozen=True)
class LexEvent:
    kind: Literal["field_value", "uncertainty"]
    start: int
    end: int  # 半开区间 [start, end)
    key: str | None = None
    code: UncertaintyCode | None = None
    detail: str = ""


class _State(Enum):
    OUT = "out"
    KEY = "key"
    AWAIT_COLON = "await_colon"
    VSTR = "vstr"
    VOTHER = "vother"


@dataclass
class _Frame:
    kind: Literal["{", "["]
    state: Literal["await_key", "await_value", "after_value"]
    sensitive: bool = False  # object 中最近一次 key 是否敏感


@dataclass
class FeedResult:
    events: list[LexEvent] = field(default_factory=list)
    safe_pos: int = 0


_VALID_SIMPLE_ESCAPES = set('"\\/bfnrt')
_HEX = set("0123456789abcdefABCDEF")


class StreamingJsonLexer:
    """跨 chunk 的字段证据扫描器。

    用法::

        lex = StreamingJsonLexer(frozenset({"password"}), 256)
        r1 = lex.feed(chunk1, 0)
        r2 = lex.feed(chunk2, len(chunk1))
        tail_events = lex.finalize(total_len)
        safe_boundary = lex.safe_pos
    """

    def __init__(self, sensitive_keys: frozenset[str], max_value_length: int) -> None:
        self._sensitive = {k.lower() for k in sensitive_keys}
        self._max_value_length = max_value_length
        self._stack: list[_Frame] = []
        self._state = _State.OUT
        # 当前 token
        self._token_start = 0
        self._key_raw_parts: list[str] = []   # 仅 KEY 累积（键短，需要解码）
        self._token_len = 0                    # VSTR/VOTHER 只计长，不存内容
        # 最近解析出的键
        self._key_start = 0
        self._pending_sensitive = False
        self._pending_key_name: str | None = None
        # 当前值
        self._value_sensitive = False
        self._value_key_name: str | None = None
        self._overlong_reported = False
        # 转义跨块状态：None / "char"（吃一个普通转义符）/ "unicode"（吃 4 位 hex）
        self._escape_mode: str | None = None
        self._unicode_left = 0
        self._disabled = False
        self.safe_pos = 0

    @property
    def held_from(self) -> int:
        """当前因结构判定未完成而必须整体扣留的最早位置。

        - 正在读取键（KEY）：键起点（键后可能跟敏感值）；
        - 敏感键等待冒号/值（AWAIT_COLON）：键起点；
        - 正在读取值（VSTR/VOTHER）且该值敏感：值起点；
        其余情况返回当前 safe_pos。
        """
        if self._disabled:
            return self.safe_pos
        if self._state == _State.KEY:
            return self._token_start
        if self._state == _State.AWAIT_COLON and self._pending_sensitive:
            return self._key_start
        if self._state in (_State.VSTR, _State.VOTHER) \
                and self._value_sensitive:
            return self._token_start
        return self.safe_pos

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def feed(self, text: str, offset: int) -> FeedResult:
        result = FeedResult(safe_pos=self.safe_pos)
        if self._disabled:
            self.safe_pos = offset + len(text)
            result.safe_pos = self.safe_pos
            return result

        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            pos = offset + i

            if self._state == _State.OUT:
                i = self._on_out(text, i, pos, result)
            elif self._state == _State.KEY:
                i = self._on_key(text, i, pos, result)
            elif self._state == _State.AWAIT_COLON:
                i = self._on_await_colon(ch, pos, result, i)
            elif self._state == _State.VSTR:
                i = self._on_vstr(text, i, pos, result)
            elif self._state == _State.VOTHER:
                i = self._on_vother(text, i, pos, result)
            else:  # pragma: no cover
                i += 1

            if self._disabled:
                self.safe_pos = offset + n
                i = n
        result.safe_pos = self.safe_pos
        return result

    def finalize(self, total_len: int) -> list[LexEvent]:
        events: list[LexEvent] = []
        if self._disabled:
            self.safe_pos = total_len
            return events
        if self._escape_mode is not None:
            # 反斜杠/unicode 在 EOF 处悬空
            events.append(LexEvent(
                "uncertainty", self._token_start, total_len,
                code="UNCLOSED_STRING",
                detail="字符串在 EOF 处存在未完成的转义序列",
            ))
        if self._state == _State.KEY:
            events.append(LexEvent(
                "uncertainty", self._token_start, total_len,
                code="UNCLOSED_STRING",
                detail="键字符串在 EOF 处未闭合",
            ))
        elif self._state == _State.VSTR:
            if self._value_sensitive:
                # 未闭合的敏感字符串：把已见到的全部内容替换掉，杜绝泄漏，
                # 同时单列不确定结论（无法证明值已完整）。
                events.append(LexEvent(
                    "field_value", self._token_start, total_len,
                    key=self._value_key_name))
            events.append(LexEvent(
                "uncertainty", self._token_start, total_len,
                code="UNCLOSED_STRING",
                detail=f"值字符串在 EOF 处未闭合（敏感={self._value_sensitive}），"
                "该尾部字段判定不完整",
            ))
        elif self._state == _State.VOTHER:
            if self._value_sensitive:
                events.append(LexEvent(
                    "field_value", self._token_start, total_len,
                    key=self._value_key_name))
                events.append(LexEvent(
                    "uncertainty", self._token_start, total_len,
                    code="UNCLOSED_STRING",
                    detail="非标量敏感值在 EOF 处未结束，已按敏感值替换"))
        elif self._state == _State.AWAIT_COLON:
            events.append(LexEvent(
                "uncertainty", self._key_start, total_len,
                code="UNCLOSED_STRING",
                detail="键在 EOF 处缺少 ':' 与值"))
        self.safe_pos = total_len
        return events

    # ------------------------------------------------------------------ #
    # OUT
    # ------------------------------------------------------------------ #
    def _on_out(self, text: str, i: int, pos: int, result: FeedResult) -> int:
        ch = text[i]
        if not self._stack:
            # 顶层散文（非 JSON 日志的正常路径）：透传
            if ch in "{[":
                self._stack.append(
                    _Frame(ch, "await_key" if ch == "{" else "await_value"))
            self.safe_pos = pos + 1
            return i + 1

        top = self._stack[-1]
        if ch in "{[":
            self._stack.append(
                _Frame(ch, "await_key" if ch == "{" else "await_value"))
            self.safe_pos = pos + 1
            return i + 1
        if ch in "}]":
            return self._on_close(ch, pos, result, i)
        if ch == ",":
            if top.state != "after_value":
                return self._invalidate(
                    result, pos, "STRUCTURAL_INVALID",
                    f"意外的 ','（帧状态={top.state}）", i, len(text))
            top.state = "await_key" if top.kind == "{" else "await_value"
            self.safe_pos = pos + 1
            return i + 1
        if ch in " \t\r\n":
            self.safe_pos = pos + 1
            return i + 1
        if ch == '"':
            if top.kind == "{" and top.state == "await_key":
                self._state = _State.KEY
                self._token_start = pos
                self._key_raw_parts = ['"']
                self._token_len = 1
                return i + 1
            if top.state == "await_value":
                self._begin_value(pos, quoted=True)
                return i + 1
            return self._invalidate(
                result, pos, "STRUCTURAL_INVALID",
                f"意外的字符串（帧={top.kind}/{top.state}）", i, len(text))
        if ch in "-0123456789tfn" and top.state == "await_value":
            self._begin_value(pos, quoted=False)
            self._token_len = 1
            # 敏感标量值：首字符起即扣住不放
            if self._value_sensitive:
                self.safe_pos = pos
            return i + 1
        return self._invalidate(
            result, pos, "STRUCTURAL_INVALID",
            f"结构上下文中的非法字符 {ch!r}", i, len(text))

    def _on_close(self, ch: str, pos: int, result: FeedResult, i: int) -> int:
        top = self._stack[-1]
        want = "}" if top.kind == "{" else "]"
        if ch != want:
            return self._invalidate(
                result, pos, "STRUCTURAL_INVALID",
                f"括号不匹配：期望 {want} 实际 {ch}", i, len(text))
        self._stack.pop()
        if self._stack and self._stack[-1].state == "await_value":
            self._stack[-1].state = "after_value"
        self.safe_pos = pos + 1
        return i + 1

    def _on_await_colon(self, ch: str, pos: int, result: FeedResult,
                        i: int) -> int:
        if ch in " \t\r\n":
            return i + 1
        if ch == ":":
            top = self._stack[-1]
            top.state = "await_value"
            top.sensitive = self._pending_sensitive
            self._state = _State.OUT
            if not self._pending_sensitive:
                self.safe_pos = pos + 1
            # 敏感键：safe_pos 停在值起点之前（冒号位置），不回退
            return i + 1
        return self._invalidate(
            result, pos, "STRUCTURAL_INVALID",
            f"键后应为 ':'，实际为 {ch!r}", i, 0)

    # ------------------------------------------------------------------ #
    # KEY / VSTR
    # ------------------------------------------------------------------ #
    def _on_key(self, text: str, i: int, pos: int, result: FeedResult) -> int:
        i = self._consume_string_char(text, i, pos, result, sensitive=False)
        if self._disabled:
            return len(text)
        # 闭合引号已在 consume 内处理
        return i

    def _on_vstr(self, text: str, i: int, pos: int, result: FeedResult) -> int:
        if self._state != _State.VSTR:
            return i  # 字符串在本字符处闭合，交回主循环处理后续
        i = self._consume_string_char(text, i, pos, result,
                                      sensitive=self._value_sensitive)
        return i

    def _consume_string_char(self, text: str, i: int, pos: int,
                             result: FeedResult, *, sensitive: bool) -> int:
        """处理 KEY/VSTR 中的一个字符（含跨块转义状态）。"""
        ch = text[i]

        # 续接跨 chunk 的转义
        if self._escape_mode == "unicode":
            if ch not in _HEX:
                return self._invalidate(
                    result, pos, "INVALID_ESCAPE",
                    r"非法 \uXXXX 转义：非十六进制字符", i, len(text))
            self._unicode_left -= 1
            if self._unicode_left == 0:
                self._escape_mode = None
            self._note_token_char(ch, sensitive, pos, result, escaped=True)
            return i + 1
        if self._escape_mode == "char":
            if ch == "u":
                self._escape_mode = "unicode"
                self._unicode_left = 4
                self._note_token_char(ch, sensitive, pos, result, escaped=True)
                return i + 1
            if ch not in _VALID_SIMPLE_ESCAPES:
                return self._invalidate(
                    result, pos, "INVALID_ESCAPE",
                    f"非法 JSON 转义字符 \\{ch!r}，字段识别停用", i, len(text))
            self._escape_mode = None
            self._note_token_char(ch, sensitive, pos, result, escaped=True)
            return i + 1

        if ch == "\\":
            # 被转义字符可能在下一 chunk：进入挂起模式
            self._escape_mode = "char"
            self._note_token_char(ch, sensitive, pos, result, escaped=True)
            return i + 1
        if ch == '"':
            end = pos + 1
            if self._state == _State.KEY:
                self._finish_key(end)
            else:
                self._finish_vstr(end, result)
            return i + 1
        self._note_token_char(ch, sensitive, pos, result, escaped=False)
        return i + 1

    def _note_token_char(self, ch: str, sensitive: bool, pos: int,
                         result: FeedResult, *, escaped: bool) -> None:
        self._token_len += 1
        if self._state == _State.KEY:
            self._key_raw_parts.append(ch)
            self.safe_pos = pos + 1
            return
        # VSTR
        if not sensitive:
            # 非敏感值可以边读边放行；处于转义挂起时保守多留 1 个字符
            if self._escape_mode is None and not escaped:
                self.safe_pos = pos + 1
        if self._token_len > self._max_value_length and not self._overlong_reported:
            self._overlong_reported = True
            result.events.append(LexEvent(
                "uncertainty", self._token_start, pos + 1,
                code="OVERLONG_FIELD_VALUE",
                detail=f"值原始长度超过配置上限 {self._max_value_length}，"
                "继续扫描界定区间并标记不确定"))

    def _finish_key(self, end: int) -> None:
        # parts 含开引号与正文（闭合引号未 append），补齐供 json 解码
        raw = "".join(self._key_raw_parts)
        if not raw.endswith('"'):
            raw += '"'
        self._key_start = self._token_start
        try:
            decoded = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            decoded = ""
        self._pending_key_name = decoded
        self._pending_sensitive = decoded.lower() in self._sensitive
        self._state = _State.AWAIT_COLON
        if not self._pending_sensitive:
            self.safe_pos = end
        # 敏感键：safe 边界扣在键起点（键文本本身发出与否不影响值安全，
        # 但保守整体扣住，直到值闭合一次性替换）

    def _begin_value(self, pos: int, *, quoted: bool) -> None:
        self._value_sensitive = (
            bool(self._stack)
            and self._stack[-1].kind == "{"
            and self._stack[-1].state == "await_value"
            and self._stack[-1].sensitive
        )
        self._value_key_name = self._pending_key_name
        self._overlong_reported = False
        self._escape_mode = None
        self._unicode_left = 0
        self._token_start = pos
        self._token_len = 1
        if quoted:
            self._state = _State.VSTR
        else:
            self._state = _State.VOTHER
            if self._value_sensitive:
                self.safe_pos = pos

    def _finish_vstr(self, end: int, result: FeedResult) -> None:
        if self._value_sensitive and self._stack \
                and self._stack[-1].state == "await_value":
            result.events.append(LexEvent(
                "field_value", self._token_start, end,
                key=self._value_key_name))
        if self._stack and self._stack[-1].state == "await_value":
            self._stack[-1].state = "after_value"
        self._state = _State.OUT
        self.safe_pos = end

    # ------------------------------------------------------------------ #
    # VOTHER：数字 / true / false / null
    # ------------------------------------------------------------------ #
    def _on_vother(self, text: str, i: int, pos: int, result: FeedResult) -> int:
        ch = text[i]
        if ch in ",]} \t\r\n":
            end = pos  # 终结符不属于值，交回 OUT 处理
            if self._value_sensitive and self._stack \
                    and self._stack[-1].state == "await_value":
                result.events.append(LexEvent(
                    "field_value", self._token_start, end,
                    key=self._value_key_name))
            if self._stack and self._stack[-1].state == "await_value":
                self._stack[-1].state = "after_value"
            self._state = _State.OUT
            self.safe_pos = end
            return i
        if ch in "\"{[":
            return self._invalidate(
                result, pos, "STRUCTURAL_INVALID",
                "标量值中途出现结构字符", i, len(text))
        self._token_len += 1
        if self._token_len > self._max_value_length and not self._overlong_reported:
            self._overlong_reported = True
            result.events.append(LexEvent(
                "uncertainty", self._token_start, pos + 1,
                code="OVERLONG_FIELD_VALUE",
                detail=f"非标量值长度超过 {self._max_value_length}，标记不确定"))
        return i + 1

    # ------------------------------------------------------------------ #
    def _invalidate(self, result: FeedResult, pos: int,
                    code: UncertaintyCode, detail: str,
                    i: int, jump_i: int) -> int:
        """字段识别失效：上报不确定并降级为透传（模式规则仍由内核扫描）。"""
        result.events.append(LexEvent(
            "uncertainty", pos, pos + 1, code=code, detail=detail))
        self._disabled = True
        self._state = _State.OUT
        return jump_i if jump_i else i
