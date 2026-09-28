"""受控 DNS 夹具 —— 不发起任何真实 DNS 请求。

区域文件（JSON）语义见 fixtures/dns/zones.json：

.. code-block:: json

    {
      "zones": {
        "multi.test": {"records": ["8.8.8.8", "169.254.169.254"]},
        "rebind.test": {"script": [
          {"records": ["127.0.0.1"]},
          {"records": ["169.254.169.254"]}
        ]},
        "nxdomain.test": {"error": "name_not_found"},
        "slow.test": {"error": "temporary"}
      }
    }

安全相关契约：

* 内核对**每个主机只调用一次** :meth:`ControlledResolver.resolve`，
  返回的快照即连接 pinning 的唯一依据（防 TOCTOU 重绑定）；
* ``script`` 每次调用推进一格并记录序号 —— 测试据此断言“第二次解析
  返回内网地址时不会被二次查询利用”；
* 未知主机抛 :class:`ComputationFailed`（``dns.name_not_found``），
  与策略拒绝严格区分；
* :meth:`ControlledResolver.clone` 给出独立调用计数，保证运行间状态隔离；
* 解析结果经 :mod:`app.ipclass` 分类，DNS 与字面量走同一分类事实来源。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ComputationFailed, Reason, ResolvedAddress, ResolvedTarget
from .ipclass import classify_literal

_ERROR_MAP = {
    "name_not_found": Reason.DNS_NAME_NOT_FOUND,
    "temporary": Reason.DNS_TEMPORARY,
}


@dataclass
class _Zone:
    records: tuple[str, ...] = ()
    script: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    note: str = ""


@dataclass
class ResolverCall:
    host: str
    port: int
    step: int
    returned: tuple[str, ...] = ()
    error: str | None = None


class ControlledResolver:
    """按区域文件应答的解析器；线程安全。"""

    def __init__(self, zones: dict[str, _Zone], *, source: str = "<memory>") -> None:
        self._zones = dict(zones)
        self.source = source
        self._lock = threading.Lock()
        self._script_pos: dict[str, int] = {}
        self._calls: list[ResolverCall] = []

    @classmethod
    def from_file(cls, path: str | Path) -> "ControlledResolver":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(_parse_zones(data.get("zones", {})), source=str(p))

    def clone(self, overrides: dict[str, dict[str, Any]] | None = None) -> "ControlledResolver":
        """复制区域、重置调用计数；``overrides`` 按主机合并附加区域。"""
        zones = dict(self._zones)
        if overrides:
            extra = _parse_zones(overrides)
            zones.update(extra)
        return ControlledResolver(zones, source=self.source + ":clone")

    def add_zone(self, host: str, spec: dict[str, Any]) -> None:
        with self._lock:
            self._zones[host] = _parse_zones({host: spec})[host]

    @property
    def calls(self) -> tuple[ResolverCall, ...]:
        with self._lock:
            return tuple(self._calls)

    def resolve(self, host: str, port: int, *, host_kind: str) -> ResolvedTarget:
        if host_kind in ("ipv4", "ipv6"):
            return self._literal(host, port)
        with self._lock:
            zone = self._zones.get(host)
            if zone is None:
                self._calls.append(ResolverCall(host, port, 0, error="name_not_found"))
                raise ComputationFailed(
                    Reason.DNS_NAME_NOT_FOUND,
                    f"受控 DNS 无此主机的夹具记录: {host}",
                    {"host": host},
                )
            if zone.error:
                reason = _ERROR_MAP.get(zone.error, Reason.DNS_TEMPORARY)
                self._calls.append(ResolverCall(host, port, len(self._calls), error=zone.error))
                raise ComputationFailed(reason, f"DNS 夹具注入错误: {zone.error}", {"host": host})

            step = self._script_pos.get(host, 0)
            if zone.script:
                frame = zone.script[min(step, len(zone.script) - 1)]
                # 脚本耗尽后停在最后一格（而不是抛错），使“重绑定第二次不同”可被观察
                self._script_pos[host] = step + 1
                if frame.get("error"):
                    reason = _ERROR_MAP.get(frame["error"], Reason.DNS_TEMPORARY)
                    self._calls.append(ResolverCall(host, port, step, error=frame["error"]))
                    raise ComputationFailed(reason, f"DNS 脚本注入错误: {frame['error']}", {"host": host, "step": step})
                literals = tuple(frame["records"])
            else:
                literals = zone.records

            addresses = tuple(self._classify(l) for l in literals)
            if not addresses:
                raise ComputationFailed(
                    Reason.DNS_NO_ADDRESS,
                    f"DNS 夹具返回空记录集: {host}",
                    {"host": host, "step": step},
                )
            self._calls.append(ResolverCall(host, port, step, returned=literals))
            attempts = len(self._calls)
        return ResolvedTarget(
            host=host,
            port=port,
            addresses=addresses,
            source="controlled_dns",
            lookup_attempts=attempts,
        )

    def _literal(self, host: str, port: int) -> ResolvedTarget:
        addr = classify_literal(host)
        return ResolvedTarget(
            host=addr.canonical_ip,
            port=port,
            addresses=(addr,),
            source="literal",
            lookup_attempts=0,
        )

    @staticmethod
    def _classify(literal: str) -> ResolvedAddress:
        try:
            return classify_literal(literal)
        except ValueError as exc:
            raise ComputationFailed(
                Reason.DNS_TEMPORARY,
                f"DNS 夹具记录了非法 IP: {literal!r}",
                {"literal": literal},
            ) from exc


def _parse_zones(raw: dict[str, dict[str, Any]]) -> dict[str, _Zone]:
    if not isinstance(raw, dict):
        raise ValueError("zones 必须是对象 {host: spec}")
    zones: dict[str, _Zone] = {}
    for host, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"区域 {host} 的定义必须是对象")
        if "error" in spec:
            if spec["error"] not in _ERROR_MAP:
                raise ValueError(f"区域 {host} 的 error 未知: {spec['error']}")
            zones[host] = _Zone(error=spec["error"], note=str(spec.get("note", "")))
        elif "script" in spec:
            frames = tuple(spec["script"])
            for f in frames:
                if "records" in f and "error" in f:
                    raise ValueError(f"区域 {host} 脚本帧不能同时含 records 与 error")
            zones[host] = _Zone(script=frames, note=str(spec.get("note", "")))
        else:
            recs = tuple(spec.get("records", []))
            if not recs:
                raise ValueError(f"区域 {host} 必须提供 records/script/error 之一")
            zones[host] = _Zone(records=recs, note=str(spec.get("note", "")))
    return zones
