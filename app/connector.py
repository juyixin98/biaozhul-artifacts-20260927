"""固定到已校验 IP 的连接原语。

关键安全性质：

* :meth:`PinnedConnector.connect` **只接收数字 IP**，内部绝不调用
  ``getaddrinfo`` —— 不会发生“校验时一个地址、连接时又解析一次”；
* 连接建立后读取实际对端 ``getpeername()``，与策略选定的 canonical IP
  逐字节比较；不一致抛 :class:`StateConflict`（``connect.pin_mismatch``）；
* TLS 仅用于对 demo 本地 HTTPS 服务：SNI/证书名校验仍用**主机名**
  （否则证书无法匹配），但 TCP 目标是固定 IP；CA 由本地合成 PKI 提供；
* 连接级失败按原因码区分：拒绝/不可达/重置/超时/证书。
"""
from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import (
    ComputationFailed,
    ParsedTarget,
    Reason,
    ResourceExhausted,
    StateConflict,
)
from .policy import AddressDecision


@dataclass
class PinnedConnection:
    sock: socket.socket
    peer_ip: str
    peer_port: int
    tls: bool
    transport: str  # "tcp" | "tls"

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Connector(Protocol):
    def connect(
        self,
        target: ParsedTarget,
        chosen: AddressDecision,
        *,
        timeout: float,
        tls_context: ssl.SSLContext | None = None,
    ) -> PinnedConnection: ...


def canonical_ip_str(raw: str) -> str:
    """对端地址规范化（压缩/等价形式），用于 pin 比较。"""
    import ipaddress

    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw


class PinnedConnector:
    """真实 socket 连接器（仅本地演示/测试使用）。"""

    def connect(
        self,
        target: ParsedTarget,
        chosen: AddressDecision,
        *,
        timeout: float,
        tls_context: ssl.SSLContext | None = None,
    ) -> PinnedConnection:
        pinned = chosen.ip
        family = socket.AF_INET if chosen.family == "ipv4" else socket.AF_INET6
        raw_sock = socket.socket(family, socket.SOCK_STREAM)
        raw_sock.settimeout(timeout)
        try:
            # 直接连固定数字 IP，绝不重新解析主机名
            raw_sock.connect((pinned, target.port))
        except TimeoutError as exc:
            raw_sock.close()
            raise ResourceExhausted(
                Reason.TIMEOUT_BUDGET,
                f"连接 {pinned}:{target.port} 超时",
                {"ip": pinned, "port": target.port, "timeout_s": timeout},
            ) from exc
        except ConnectionRefusedError as exc:
            raw_sock.close()
            raise ComputationFailed(
                Reason.CONNECT_REFUSED,
                f"连接被拒绝 {pinned}:{target.port}",
                {"ip": pinned, "port": target.port},
            ) from exc
        except OSError as exc:
            raw_sock.close()
            # EHOSTUNREACH/ENETUNREACH 等
            reason = Reason.CONNECT_UNREACHABLE
            raise ComputationFailed(
                reason, f"无法连接 {pinned}:{target.port}: {exc}", {"ip": pinned, "port": target.port}
            ) from exc

        peer = self._verified_peer(raw_sock, pinned, target.port)

        sock = raw_sock
        transport = "tcp"
        if target.scheme == "https":
            ctx = tls_context or _strict_default_context()
            try:
                # SNI 与证书校验用主机名；TCP 目标仍是固定 IP
                sock = ctx.wrap_socket(raw_sock, server_hostname=target.host)
            except ssl.SSLCertVerificationError as exc:
                raw_sock.close()
                raise ComputationFailed(
                    Reason.TLS_CERT_VERIFY,
                    f"TLS 证书校验失败: {exc.verify_message}",
                    {"host": target.host, "ip": pinned},
                ) from exc
            except (ssl.SSLError, OSError) as exc:
                raw_sock.close()
                raise ComputationFailed(
                    Reason.TLS_OTHER, f"TLS 握手失败: {exc}", {"host": target.host, "ip": pinned}
                ) from exc
            transport = "tls"

        return PinnedConnection(sock=sock, peer_ip=peer, peer_port=target.port, tls=transport == "tls", transport=transport)

    @staticmethod
    def _verified_peer(sock: socket.socket, pinned: str, port: int) -> str:
        try:
            name = sock.getpeername()
        except OSError as exc:
            raise StateConflict(
                Reason.PIN_MISMATCH,
                "连接建立后无法读取对端地址",
                {"pinned_ip": pinned},
            ) from exc
        peer_ip = canonical_ip_str(name[0])
        if peer_ip != canonical_ip_str(pinned):
            # 理论上 connect((ip,port)) 不可能连到别的 IP；此校验是纵深防御，
            # 也让“伪造连接器/透明代理”导致的偏差以 STATE_CONFLICT 显式暴露
            raise StateConflict(
                Reason.PIN_MISMATCH,
                "实际对端 IP 与策略固定地址不一致",
                {"pinned_ip": pinned, "peer_ip": peer_ip, "peer_port": name[1]},
            )
        return peer_ip


def _strict_default_context() -> ssl.SSLContext:
    """无自定义 CA 时的兜底：系统信任库（演示环境应传入本地 CA）。"""
    return ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)


@dataclass
class AttemptRecord:
    ip: str
    port: int
    transport: str
    host_header: str
    result: str  # "opened" | "blocked_preconnect" | "error"
    detail: dict[str, Any]


class RecordingConnector:
    """包装任意连接器，记录每次连接尝试（测试断言“禁止地址从未被连接”）。

    若 ``permit`` 非空，则任何对不在允许集合中 IP 的连接在**发包前**
    直接抛 :class:`StateConflict`，作为测试夹具的第二道防线。
    """

    def __init__(
        self,
        inner: Connector | None = None,
        *,
        permit: frozenset[str] = frozenset(),
    ) -> None:
        self.inner = inner or PinnedConnector()
        self.permit = frozenset(canonical_ip_str(i) for i in permit)
        self.attempts: list[AttemptRecord] = []

    def connect(
        self,
        target: ParsedTarget,
        chosen: AddressDecision,
        *,
        timeout: float,
        tls_context: ssl.SSLContext | None = None,
    ) -> PinnedConnection:
        ip = canonical_ip_str(chosen.ip)
        host_header = target.host if target.host_kind == "dns" else chosen.ip
        if self.permit and ip not in self.permit:
            self.attempts.append(
                AttemptRecord(ip, target.port, "n/a", host_header, "blocked_preconnect",
                              {"reason": "ip not in fixture permit set"})
            )
            raise StateConflict(
                Reason.PIN_MISMATCH,
                "RecordingConnector 在发包前拦截：目标 IP 不在夹具允许集合",
                {"ip": ip, "permit": sorted(self.permit)},
            )
        try:
            conn = self.inner.connect(target, chosen, timeout=timeout, tls_context=tls_context)
        except Exception as exc:
            self.attempts.append(
                AttemptRecord(ip, target.port, "n/a", host_header, "error",
                              {"error": type(exc).__name__})
            )
            raise
        self.attempts.append(
            AttemptRecord(ip, target.port, conn.transport, host_header, "opened",
                          {"peer_ip": conn.peer_ip})
        )
        return conn
