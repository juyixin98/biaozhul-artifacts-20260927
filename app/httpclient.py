"""最小 HTTP/1.1 客户端：在**已固定 IP 的 socket** 上手工收发。

不使用 requests/httpx 的连接层 —— 那些库会自行 getaddrinfo，
无法保证“校验地址 == 实际连接地址”。本模块：

* 请求行路径使用已规范化 path/query；``Host`` 头用原始主机名（虚拟主机）；
* 仅实现 GET、identity/chunked 两种响应体读取；
* 读取超过 ``max_bytes`` 立即中断并抛 :class:`ResourceExhausted`；
* 3xx 只回传 Location，是否跟随由**内核每跳重新解析+重新校验**决定；
* 协议异常统一 :class:`ComputationFailed`（``http.protocol``）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .connector import PinnedConnection
from .contracts import ComputationFailed, ParsedTarget, Reason, ResourceExhausted

_REDIRECT_STATUS = {301, 302, 303, 307, 308}


@dataclass
class FetchResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes
    body_truncated: bool = False
    transport: str = "tcp"
    peer_ip: str = ""

    @property
    def is_redirect(self) -> bool:
        return self.status in _REDIRECT_STATUS

    @property
    def location(self) -> str | None:
        return self.headers.get("location")

    def to_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "reason": self.reason,
            "headers": self.headers,
            "body_bytes": len(self.body),
            "body_truncated": self.body_truncated,
            "transport": self.transport,
            "peer_ip": self.peer_ip,
        }
        if include_body:
            try:
                out["body_preview"] = self.body[:512].decode("utf-8", "replace")
            except Exception:
                out["body_preview"] = ""
        return out


def write_request(conn: PinnedConnection, target: ParsedTarget) -> None:
    path = target.path
    from urllib.parse import urlsplit

    # target.url 已含规范化 path?query
    parts = urlsplit(target.url)
    if parts.query:
        path = f"{parts.path}?{parts.query}"
    host_header = target.host if target.host_kind == "dns" else target.host
    if target.port not in (80, 443):
        host_header = f"{host_header}:{target.port}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "User-Agent: ssrf-guard/1.0\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    try:
        conn.sock.sendall(request.encode("ascii"))
    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        raise ComputationFailed(
            Reason.CONNECT_RESET, f"发送请求失败: {exc}", {"ip": conn.peer_ip}
        ) from exc


def read_response(conn: PinnedConnection, *, max_bytes: int) -> FetchResponse:
    sock = conn.sock
    chunks: list[bytes] = []
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            more = sock.recv(65536)
            if not more:
                break
            buf += more
            if len(buf) > 64 * 1024 and b"\r\n\r\n" not in buf:
                raise ComputationFailed(Reason.HTTP_PROTOCOL, "响应头超过 64KiB", {"got": len(buf)})
        header_bytes, _, rest = buf.partition(b"\r\n\r\n")
        if not header_bytes:
            raise ComputationFailed(Reason.HTTP_PROTOCOL, "对端关闭且未返回任何字节", {})
        status_line, *header_lines = header_bytes.split(b"\r\n")
        try:
            version, status_text, reason = status_line.split(b" ", 2)
            status = int(status_text)
        except ValueError as exc:
            raise ComputationFailed(
                Reason.HTTP_PROTOCOL, f"非法状态行: {status_line[:64]!r}", {}
            ) from exc

        headers: dict[str, str] = {}
        for line in header_lines:
            try:
                name, value = line.split(b":", 1)
            except ValueError:
                continue
            headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()

        te = headers.get("transfer-encoding", "").lower()
        cl = headers.get("content-length")
        body, truncated = _read_body(sock, rest, te=te, content_length=cl, max_bytes=max_bytes)
        return FetchResponse(
            status=status,
            reason=reason.decode("latin-1", "replace"),
            headers=headers,
            body=body,
            body_truncated=truncated,
            transport=conn.transport,
            peer_ip=conn.peer_ip,
        )
    except ComputationFailed:
        raise
    except (ConnectionResetError, OSError) as exc:
        raise ComputationFailed(
            Reason.HTTP_PROTOCOL, f"读取响应时连接异常: {exc}", {"ip": conn.peer_ip}
        ) from exc


def _read_body(
    sock, initial: bytes, *, te: str, content_length: str | None, max_bytes: int
) -> tuple[bytes, bool]:
    if "chunked" in te:
        return _read_chunked(sock, initial, max_bytes=max_bytes)
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError as exc:
            raise ComputationFailed(Reason.HTTP_PROTOCOL, f"非法 Content-Length: {content_length!r}", {}) from exc
        if length > max_bytes:
            raise ResourceExhausted(
                Reason.RESPONSE_TOO_LARGE,
                f"声明响应体 {length} 字节超过上限 {max_bytes}",
                {"declared": length, "max_bytes": max_bytes},
            )
        body = bytearray(initial)
        while len(body) < length:
            more = sock.recv(min(65536, length - len(body)))
            if not more:
                break
            body += more
            if len(body) > max_bytes:
                raise ResourceExhausted(
                    Reason.RESPONSE_TOO_LARGE,
                    "响应体超过读取上限",
                    {"read": len(body), "max_bytes": max_bytes},
                )
        return bytes(body[:length]), len(body) < length

    # 无长度信息：读到连接关闭
    body = bytearray(initial)
    truncated = False
    while True:
        more = sock.recv(65536)
        if not more:
            break
        body += more
        if len(body) > max_bytes:
            truncated = True
            body = body[:max_bytes]
            break
    return bytes(body), truncated


def _read_chunked(sock, initial: bytes, *, max_bytes: int) -> tuple[bytes, bool]:
    data = initial
    body = bytearray()
    truncated = False
    while True:
        line, data = _read_line(sock, data)
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise ComputationFailed(Reason.HTTP_PROTOCOL, f"非法 chunk 长度: {line[:32]!r}", {}) from exc
        if size == 0:
            # 读到 trailer 结束
            while True:
                tline, data = _read_line(sock, data)
                if not tline:
                    break
            break
        while len(data) < size + 2:  # +CRLF
            more = sock.recv(65536)
            if not more:
                raise ComputationFailed(Reason.HTTP_PROTOCOL, "chunked 响应提前结束", {})
            data += more
        body += data[:size]
        data = data[size + 2 :]
        if len(body) > max_bytes:
            truncated = True
            body = body[:max_bytes]
            break
    return bytes(body), truncated


def _read_line(sock, data: bytes) -> tuple[bytes, bytes]:
    while b"\r\n" not in data:
        more = sock.recv(65536)
        if not more:
            raise ComputationFailed(Reason.HTTP_PROTOCOL, "分块响应缺少 CRLF", {})
        data += more
    line, rest = data.split(b"\r\n", 1)
    return line, rest
