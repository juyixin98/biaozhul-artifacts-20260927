"""受控 DNS 夹具 —— 合成 zone 文件 + 可回放解析器。

为什么需要它
============
真实 DNS 不可控、会走公网、无法确定性复现重绑定。安全测试必须把"域名 →
地址"这一步变成**夹具驱动**：

* zone 文件是本地合成证据，人类可读、可评审（见 fixtures/dns/*.zone）；
* 解析器**按 zone 中答案出现的顺序**返回，``sequence`` 语义让重绑定
  （第一次公网、第二次内网）可以被确定性回放；
* 每次解析都记录游标推进，决策链可展示"第 1 次解析=允许地址、第 2 次
  解析=禁止地址"；
* 解析数量受 ``max_dns_answers`` 预算限制（资源耗尽可区分）；
* 支持 A / AAAA；AAAA 可写成 mapped 形态以测试解包。

zone 行语法（每行一条，``#`` 注释，空行忽略）::

    <name>  A     <ipv4>
    <name>  AAAA  <ipv6>

同一名字多行即多个答案，顺序即返回顺序。
"""

from __future__ import annotations

import re
from typing import Protocol

from ..contracts import IpCandidate
from ..errors import DnsAnswerBudgetError, DnsResolutionError, ZoneFileError
from .addrip import canonicalize_ip_literal

_LINE_RE = re.compile(
    r"^(?P<name>[a-z0-9.-]+)\s+(?P<type>A|AAAA)\s+(?P<addr>\S+)\s*(?:#.*)?$"
)


class Resolver(Protocol):
    """解析器协议：内核只依赖这一个方法，可替换为假实现/真实实现。"""

    def resolve(self, host: str, budget: int) -> tuple[IpCandidate, ...]:
        """返回全部候选（顺序即连接尝试顺序）。未知主机抛 DnsResolutionError。"""
        ...

    def sequence_cursor(self, host: str) -> int:
        """该主机已经被取走多少条答案（重绑定回放证据）。"""
        ...


def parse_zone(path: str) -> dict[str, list[IpCandidate]]:
    """解析合成 zone 文件为 ``{host: [候选...]}``（不做游标，只做数据）。"""

    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise ZoneFileError(f"无法读取 zone 文件 {path}: {exc}", details={"path": path}) from exc

    table: dict[str, list[IpCandidate]] = {}
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            raise ZoneFileError(
                f"zone 第 {lineno} 行语法非法: {line!r}",
                details={"path": path, "line": lineno},
            )
        name = m.group("name").lower().strip(".")
        addr = m.group("addr")
        try:
            canonical, family, shape = canonicalize_ip_literal(addr)
        except Exception as exc:  # noqa: BLE001 - 统一转为 zone 错误
            raise ZoneFileError(
                f"zone 第 {lineno} 行地址非法: {addr!r}: {exc}",
                details={"path": path, "line": lineno},
            ) from exc
        ordinal = len(table.get(name, []))
        table.setdefault(name, []).append(
            IpCandidate(literal=canonical, family=family, source="dns", ordinal=ordinal)
        )
    if not table:
        raise ZoneFileError("zone 文件不含任何记录", details={"path": path})
    return table


class FixtureResolver:
    """夹具解析器。

    * 普通主机：每次返回全部答案（游标仅用于证据展示）；
    * ``rebind`` 语义通过在不同跳之间由内核再次调用 ``resolve`` 体现——
      夹具按**轮转**方式在多答案间切换，从而第 1 跳与第 2 跳看到不同地址；
    """

    def __init__(
        self,
        table: dict[str, list[IpCandidate]],
        *,
        rotate: bool = True,
    ) -> None:
        self._table = table
        self._rotate = rotate
        self._cursors: dict[str, int] = {}
        self._call_log: list[tuple[str, tuple[str, ...]]] = []

    def resolve(self, host: str, budget: int) -> tuple[IpCandidate, ...]:
        key = host.lower().strip(".")
        answers = self._table.get(key)
        if not answers:
            raise DnsResolutionError(
                f"夹具 zone 中没有主机 {host!r} 的记录",
                details={"host": host},
            )
        if len(answers) > budget:
            raise DnsAnswerBudgetError(
                f"{host} 有 {len(answers)} 条答案，超过预算 {budget}",
                details={"host": host, "answers": len(answers), "budget": budget},
            )

        cursor = self._cursors.get(key, 0)
        if self._rotate and len(answers) > 1:
            # 轮转：每次调用从不同答案开始（确定性重绑定），但始终返回全部候选，
            # 由"混合集判定"保证：哪怕第一个是公网，集合里只要含内网就整体拒绝。
            ordered = tuple(answers[(cursor + i) % len(answers)] for i in range(len(answers)))
        else:
            ordered = tuple(answers)
        self._cursors[key] = cursor + 1
        self._call_log.append((key, tuple(c.literal for c in ordered)))
        return ordered

    def sequence_cursor(self, host: str) -> int:
        return self._cursors.get(host.lower().strip("."), 0)

    def call_log(self) -> list[tuple[str, tuple[str, ...]]]:
        return list(self._call_log)


class StaticResolver:
    """单条内存映射的解析器（测试用最小构造）。"""

    def __init__(self, mapping: dict[str, list[str]]):
        table: dict[str, list[IpCandidate]] = {}
        for host, addrs in mapping.items():
            table[host.lower().strip(".")] = [
                IpCandidate(literal=canonicalize_ip_literal(a)[0],
                            family=canonicalize_ip_literal(a)[1],
                            source="dns", ordinal=i)
                for i, a in enumerate(addrs)
            ]
        self._inner = FixtureResolver(table)

    def resolve(self, host: str, budget: int) -> tuple[IpCandidate, ...]:
        return self._inner.resolve(host, budget)

    def sequence_cursor(self, host: str) -> int:
        return self._inner.sequence_cursor(host)
