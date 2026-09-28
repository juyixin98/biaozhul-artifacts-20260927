//! Shared integration-test helpers.
//!
//! * [`spawn_server`] starts the real server binary code path on an ephemeral
//!   loopback port (nothing mocked; the full axum stack runs).
//! * [`http_request`] is a deliberately minimal HTTP/1.1 client using only
//!   [`std::net::TcpStream`] — no web-stack dependency in the test oracle.
//! * Every test records a [`diffconstraints::testlog::RunLog`] run.
//!
//! Not every helper is used by every test binary that compiles this module,
//! so dead-code warnings are disabled at module scope.
#![allow(dead_code)]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

pub mod oracle;

use diffconstraints::http_api;
use diffconstraints::ConstraintService;

pub struct ServerHandle {
    pub addr: String,
    // Kept alive for the test; dropped at end -> server stops accepting.
    _shutdown: tokio::sync::oneshot::Sender<()>,
    pub runtime: tokio::runtime::Runtime,
    pub service: Arc<ConstraintService>,
}

/// Bind an ephemeral port, serve the real app on a dedicated runtime thread.
pub fn spawn_server() -> ServerHandle {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .expect("build tokio runtime");
    let listener = runtime
        .block_on(tokio::net::TcpListener::bind("127.0.0.1:0"))
        .expect("bind ephemeral port");
    let addr = listener.local_addr().unwrap().to_string();
    let service = Arc::new(ConstraintService::new());
    let app = http_api::app(service.clone());
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    runtime.spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                let _ = rx.await;
            })
            .await
            .expect("server task");
    });
    // Tiny readiness wait via loopback connect retry.
    let start = std::time::Instant::now();
    loop {
        if std::net::TcpStream::connect_timeout(
            &addr.parse().unwrap(),
            std::time::Duration::from_millis(100),
        )
        .is_ok()
        {
            break;
        }
        if start.elapsed() > std::time::Duration::from_secs(5) {
            panic!("test server on {addr} did not become ready");
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    ServerHandle {
        addr,
        _shutdown: tx,
        runtime,
        service,
    }
}

pub struct HttpResponse {
    pub status: u16,
    pub body: String,
}

impl HttpResponse {
    pub fn json(&self) -> serde_json::Value {
        serde_json::from_str(&self.body).unwrap_or_else(|e| {
            panic!(
                "response was not JSON ({e}); status {} body: {}",
                self.status,
                &self.body[..self.body.len().min(500)]
            )
        })
    }
}

/// Minimal HTTP/1.1 request. `body` empty means GET, otherwise POST with
/// application/json.
pub fn http_request(addr: &str, method: &str, path: &str, body: &str) -> HttpResponse {
    let mut stream = TcpStream::connect(addr).expect("connect to test server");
    stream
        .set_read_timeout(Some(std::time::Duration::from_secs(10)))
        .unwrap();
    let mut req = format!(
        "{method} {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n"
    );
    if !body.is_empty() {
        req.push_str("Content-Type: application/json\r\n");
        req.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    req.push_str("\r\n");
    req.push_str(body);
    stream.write_all(req.as_bytes()).expect("write request");
    stream.flush().ok();

    let mut raw = Vec::new();
    stream
        .read_to_end(&mut raw)
        .expect("read full response (Connection: close)");
    let raw = String::from_utf8_lossy(&raw);
    let (head, body_part) = raw.split_once("\r\n\r\n").expect("http head/body split");
    let status = head
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);

    // Handle chunked transfer-encoding (hyper may use it).
    let body = if head
        .lines()
        .any(|l| l.eq_ignore_ascii_case("transfer-encoding: chunked"))
    {
        dechunk(body_part)
    } else {
        body_part.to_string()
    };
    HttpResponse { status, body }
}

pub fn get(addr: &str, path: &str) -> HttpResponse {
    http_request(addr, "GET", path, "")
}

pub fn post_json(addr: &str, path: &str, value: &serde_json::Value) -> HttpResponse {
    http_request(addr, "POST", path, &value.to_string())
}

fn dechunk(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        // read size line
        let line_start = i;
        while i < bytes.len() && bytes[i] != b'\n' {
            i += 1;
        }
        let size_line = std::str::from_utf8(&bytes[line_start..i.saturating_sub(0)])
            .unwrap_or("")
            .trim();
        if i < bytes.len() {
            i += 1; // consume \n
        }
        let size = usize::from_str_radix(size_line.trim_matches(['\r', ' ']), 16).unwrap_or(0);
        if size == 0 {
            break;
        }
        out.extend_from_slice(&bytes[i..i + size]);
        i += size;
        // trailing CRLF
        if i + 1 < bytes.len() && bytes[i] == b'\r' && bytes[i + 1] == b'\n' {
            i += 2;
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

/// Current-time based unique id, for constraint ids that must not collide.
pub fn unique(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}_{nanos:x}")
}
