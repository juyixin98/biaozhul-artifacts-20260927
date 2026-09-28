"""仅绑定本机的演示上游服务（stdlib http.server，合成、离线）。

路由：

* ``GET /ok``                -> 200 JSON
* ``GET /redirect?to=URL&n=N`` -> 302 到 to（n 为 1 时落到 /ok）
* ``GET /loop?to=URL``       -> 302（演示用它构成 A->B->A 环）
* ``GET /chain?n=N``         -> 302 链，第 N 跳到 /ok
* ``GET /file-redirect``     -> 302 到 file:///etc/passwd（方案降级测试）
* ``GET /large?bytes=N``     -> 200，固定大小正文（超限测试）
* ``GET /who``               -> 200，回显 Host/对端，用于确认连接目标
"""
from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def build_handler(hostname: str, scheme: str):
    class DemoHandler(BaseHTTPRequestHandler):
        server_version = "ssrf-demo/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 安静；演示 runner 自己输出
            pass

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            body = b""
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parts = urlsplit(self.path)
            qs = parse_qs(parts.query)
            path = parts.path
            port = self.server.server_address[1]
            authority = hostname if port in (80, 443) else f"{hostname}:{port}"
            base = f"{scheme}://{authority}"

            if path == "/ok":
                self._send_json(200, {"ok": True, "service": "demo-upstream",
                                      "host_header": self.headers.get("Host"),
                                      "peer": self.client_address[0]})
            elif path == "/who":
                self._send_json(200, {"path": path, "host_header": self.headers.get("Host"),
                                      "peer": self.client_address[0]})
            elif path == "/redirect":
                to = qs.get("to", ["/ok"])[0]
                n = int(qs.get("n", ["1"])[0])
                if n > 1:
                    target = f"{base}/redirect?to={to}&n={n - 1}"
                else:
                    target = to if to.startswith("http") else f"{base}{to}"
                self._redirect(target)
            elif path == "/loop":
                to = qs.get("to", [f"{base}/ok"])[0]
                self._redirect(to if to.startswith("http") else f"{base}{to}")
            elif path == "/loop-a":
                self._redirect(f"{base}/loop-b")
            elif path == "/loop-b":
                self._redirect(f"{base}/loop-a")
            elif path == "/chain":
                n = int(qs.get("n", ["1"])[0])
                if n > 0:
                    self._redirect(f"{base}/chain?n={n - 1}")
                else:
                    self._redirect(f"{base}/ok")
            elif path == "/file-redirect":
                self._redirect("file:///etc/passwd")
            elif path == "/large":
                size = int(qs.get("bytes", ["1024"])[0])
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                # 分块写，避免一次分配
                block = b"a" * 4096
                remaining = size
                while remaining > 0:
                    take = min(remaining, len(block))
                    self.wfile.write(block[:take])
                    remaining -= take
            else:
                self._send_json(404, {"error": "not_found", "path": path})

    return DemoHandler


class DemoUpstream:
    """后台线程中的本地 HTTP 或 HTTPS 服务。"""

    def __init__(self, *, hostname: str, scheme: str, port: int = 0,
                 ssl_context: ssl.SSLContext | None = None) -> None:
        self.hostname = hostname
        self.scheme = scheme
        handler = build_handler(hostname, scheme)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        if ssl_context is not None:
            self.httpd.socket = ssl_context.wrap_socket(self.httpd.socket, server_side=True)
            self.scheme = "https"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def base_url(self) -> str:
        return f"{self.scheme}://{self.hostname}:{self.port}"

    def start(self) -> "DemoUpstream":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
