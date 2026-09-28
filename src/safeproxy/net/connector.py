"""固定地址连接器 —— pinning 的落点。

内核的 DNS 结果是唯一真相：连接器拿到一个已批准的 ``IpCandidate``，
**直接向其字面量 IP 发起 TCP 连接**，绝不把主机名交给 ``getaddrinfo``
再次解析。否则在"校验时"和"连接时"之间存在 TOCTOU 窗口（经典重绑定：
校验返回公网 IP，连接时 DNS 已切成 169.254.169.254）。

实现
====
:class:`PinnedHTTPConnector` 自己完成最小 HTTP/1.1 交换：
``socket.create_connection((pin_ip, port))`` → 发送手写请求 → 读响应。
因为传给 socket 的是 IP 字面量，内核不再做名字解析，从根上消除二次解析。

连接建立后读取到的 ``socket.getpeername()`` 会再与 pin 比对一次（纵深
防御），不一致抛 PinMismatchError。

``Host`` 头仍使用**原始主机名**（虚拟主机/SNI 语义），但路由决策只认 pin。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

from ..contracts import IpCandidate, ParsedUrl
from ..errors import ConnectError, HttpProtocolError, PinMismatchError

_MAX_RESPONSE_HEAD = 64 * 1024


@dataclass(frozen=True, slots=True)
class RawResponse:
    status_code: int
    location: str | None
    headers: dict[str, str]
    body: bytes
    peer: tuple[str, int]


class Connector:
    """连接器协议（测试可用 RecordingConnector 替换）。"""

    def fetch(
        self,
        parsed: ParsedUrl,
        pin: IpCandidate,
        *,
        method: str = "GET",
        timeout: float = 3.0,
        max_header_bytes: int = _MAX_RESPONSE_HEAD,
        max_body_bytes: int = 1 << 20,
    ) -> RawResponse:
        raise NotImplementedError


class PinnedHTTPConnector(Connector):
    """向固定 IP 发起真实连接。仅支持 http（演示测试服务不需要 TLS）。"""

    def fetch(
        self,
        parsed: ParsedUrl,
        pin: IpCandidate,
        *,
        method: str = "GET",
        timeout: float = 3.0,
        max_header_bytes: int = _MAX_RESPONSE_HEAD,
        max_body_bytes: int = 1 << 20,
    ) -> RawResponse:
        if parsed.scheme == "https":
            # 演示栈不真实发起 TLS；结构上保留拒绝点，避免误以为已做 SNI pinning。
            raise ConnectError(
                "演示连接器不支持 https 出站（TLS pinning 未实现）",
                code="E_HTTPS_UNSUPPORTED",
                details={"host": parsed.host},
            )

        family = pin.family
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        peer: tuple[str, int] | None = None
        try:
            # 关键：传入 IP 字面量，socket 层不会再做名字解析。
            sock.connect((pin.literal, parsed.port))
            peer = sock.getpeername()
            peer_host = peer[0]
            # 连接前/后复核：对端必须就是被批准的 pin（规范化后比较）。
            if _normalize_peer(peer_host) != _normalize_peer(pin.literal):
                raise PinMismatchError(
                    "连接对端与已批准 pin 不一致（疑似二次解析/重绑定）",
                    details={
                        "pin": pin.literal,
                        "peer": peer_host,
                        "host": parsed.host,
                        "port": parsed.port,
                    },
                )
            return self._exchange(sock, parsed, method, peer, max_header_bytes, max_body_bytes)
        except socket.timeout as exc:
            raise ConnectError(
                f"连接/读取超时（{timeout}s）",
                code="E_CONNECT_TIMEOUT",
                details={"pin": pin.literal, "port": parsed.port},
            ) from exc
        except OSError as exc:
            if isinstance(exc, PinMismatchError):
                raise
            raise ConnectError(
                f"连接 {pin.literal}:{parsed.port} 失败: {exc}",
                details={"pin": pin.literal, "port": parsed.port, "errno": getattr(exc, "errno", None)},
            ) from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _exchange(
        self,
        sock: socket.socket,
        parsed: ParsedUrl,
        method: str,
        peer: tuple[str, int],
        max_header_bytes: int,
        max_body_bytes: int,
    ) -> RawResponse:
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        host_header = parsed.host if ":" not in parsed.host else f"[{parsed.host}]"
        if parsed.port not in (80, 443):
            host_header += f":{parsed.port}"
        request = (
            f"{method} {target} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "User-Agent: safeproxy-kernel/1.0\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            sock.sendall(request.encode("ascii"))
            return self._read_response(sock, peer, max_header_bytes, max_body_bytes)
        except OSError as exc:
            raise HttpProtocolError(f"读取响应失败: {exc}", details={"peer": list(peer)}) from exc

    def _read_response(
        self, sock: socket.socket, peer: tuple[str, int], max_header_bytes: int, max_body_bytes: int
    ) -> RawResponse:
        buf = bytearray()
        deadline = time.monotonic() + (sock.gettimeout() or 3.0)
        while b"\r\n\r\n" not in buf:
            if len(buf) > max_header_bytes:
                raise HttpProtocolError(
                    "响应头超过上限", code="E_HEADER_TOO_LARGE",
                    details={"limit": max_header_bytes},
                )
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if time.monotonic() > deadline:
                raise ConnectError("读取响应头超时", code="E_CONNECT_TIMEOUT")
        if b"\r\n\r\n" not in buf:
            raise HttpProtocolError("响应不完整：缺少头部终止符", code="E_HTTP_TRUNCATED")

        head_bytes, _, rest = buf.partition(b"\r\n\r\n")
        head_text = head_bytes.decode("iso-8859-1")
        lines = head_text.split("\r\n")
        status_line = lines[0]
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise HttpProtocolError(f"状态行非法: {status_line!r}", code="E_HTTP_STATUS_LINE")
        status_code = int(parts[1])

        headers: dict[str, str] = {}
        location = None
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            key = name.strip().lower()
            val = value.strip()
            headers[key] = val
            if key == "location":
                location = val

        body = bytearray(rest)
        declared = _content_length(headers)
        while True:
            if declared is not None and len(body) >= declared:
                break
            if len(body) > max_body_bytes:
                raise HttpProtocolError(
                    "响应体超过上限", code="E_BODY_TOO_LARGE",
                    details={"limit": max_body_bytes},
                )
            chunk = sock.recv(8192)
            if not chunk:
                break
            body.extend(chunk)
        if declared is not None:
            body = body[:declared]

        return RawResponse(
            status_code=status_code,
            location=location,
            headers=headers,
            body=bytes(body),
            peer=peer,
        )


def _content_length(headers: dict[str, str]) -> int | None:
    val = headers.get("content-length")
    if val is None:
        return None
    try:
        n = int(val)
    except ValueError:
        return None
    return n if n >= 0 else None


def _normalize_peer(ip: str) -> str:
    from .addrip import canonicalize_ip_literal

    try:
        return canonicalize_ip_literal(ip)[0]
    except Exception:  # noqa: BLE001
        return ip
