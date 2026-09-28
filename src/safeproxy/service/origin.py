"""本地测试源服务（origin）—— 只绑定 127.0.0.1 的合成 HTTP 源站。

它扮演"被代理访问的后端"，提供正常页与各种重定向，用于端到端演示
"每跳重校验"。**绝不绑定非环回地址**，也不向任何外部地址发起连接——
重定向目标是名字，是否可连完全由被测内核的策略决定。

路由
====
``/ok``                      200 正常业务页
``/redirect-ok``             302 → /ok（同源允许，演示正常跟随）
``/redirect-meta``           302 → http://metadata.example/（跨到被禁地址）
``/redirect-rebind``         302 → http://rebind.example/（第 2 跳解析含内网）
``/redirect-mapped``         302 → http://mapped.example/（::ffff:127.0.0.1）
``/loop-a``                 302 → /loop-b（字面量环回，允许但交叉成环）
``/loop-b``                 302 → /loop-a
``/many``                    302 → /many（自我重定向，打爆重定向预算）

端口由环境变量 ``ORIGIN_PORT`` 决定，默认 18080。
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DEFAULT_PORT = int(os.environ.get("ORIGIN_PORT", "18080"))


class _Handler(BaseHTTPRequestHandler):
    server_version = "safeproxy-origin/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 静音
        return

    def _send(self, code: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _redirect(self, location: str, note: str = "") -> None:
        self._send(302, note.encode() or b"redirect", {"Location": location})

    def do_GET(self) -> None:  # noqa: N802 - stdlib 命名
        parsed = urlparse(self.path)
        path = parsed.path
        port = self.server.server_address[1]  # type: ignore[attr-defined]
        base = f"http://127.0.0.1:{port}"

        if path == "/ok":
            self._send(200, f"origin-ok from {base}\n".encode())
        elif path == "/redirect-ok":
            self._redirect(f"{base}/ok", "same-origin redirect")
        elif path == "/redirect-meta":
            self._redirect("http://metadata.example/latest/meta-data/", "to forbidden metadata")
        elif path == "/redirect-rebind":
            self._redirect("http://rebind.example:18080/loophole", "to rebinding name")
        elif path == "/redirect-mapped":
            self._redirect("http://mapped.example/x", "to ipv4-mapped ipv6 name")
        elif path == "/many":
            self._redirect(f"{base}/many", "self redirect for budget test")
        elif path == "/loop-a":
            self._redirect(f"{base}/loop-b", "loop step A")
        elif path == "/loop-b":
            self._redirect(f"{base}/loop-a", "loop step B")
        else:
            self._send(404, b"not found\n")


class OriginServer:
    """线程内运行的本地源站，测试/演示用 with 语法管理生命周期。"""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "OriginServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


if __name__ == "__main__":  # pragma: no cover
    srv = OriginServer(DEFAULT_PORT)
    print(f"[origin] listening only on 127.0.0.1:{DEFAULT_PORT}", flush=True)
    try:
        srv._httpd.serve_forever()
    except KeyboardInterrupt:
        pass
