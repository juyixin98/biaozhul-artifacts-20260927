//! Evidence 06 — HTTP validation interface. The server runs in-process on an
//! ephemeral port; the client is a tiny std-only HTTP/1.1 caller (no test HTTP
//! dependency). We assert concrete status codes, the four error categories,
//! run-id propagation, and an end-to-end core/reference cross-check request.

mod common;

use common::*;
use lz77b::http::handlers::AppState;
use lz77b::store::BlockStore;

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

struct ServerGuard {
    addr: SocketAddr,
    shutdown: Option<tokio::sync::oneshot::Sender<()>>,
    thread: Option<std::thread::JoinHandle<()>>,
}

fn start_server() -> (ServerGuard, std::path::PathBuf) {
    let dir = tempdir("http");
    let store = BlockStore::open(&dir).unwrap();
    let state = AppState::new(store);
    let app = lz77b::http::router(state);

    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    let (addr_tx, addr_rx) = std::sync::mpsc::channel::<SocketAddr>();

    let thread = std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            // The Tokio TCP listener must be created inside the runtime.
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            addr_tx.send(listener.local_addr().unwrap()).unwrap();
            let server = axum::serve(listener, app).with_graceful_shutdown(async {
                let _ = rx.await;
            });
            server.await.unwrap();
        });
    });
    let addr = addr_rx.recv().unwrap();
    (
        ServerGuard {
            addr,
            shutdown: Some(tx),
            thread: Some(thread),
        },
        dir,
    )
}

impl Drop for ServerGuard {
    fn drop(&mut self) {
        if let Some(tx) = self.shutdown.take() {
            let _ = tx.send(());
        }
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

struct Response {
    status: u16,
    body: String,
    run_id_header: Option<String>,
}

fn request(
    addr: SocketAddr,
    method: &str,
    path: &str,
    body: Option<&str>,
    run_header: Option<&str>,
) -> Response {
    let mut last_err = None;
    for attempt in 0..50 {
        match TcpStream::connect(addr) {
            Ok(mut stream) => {
                stream
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                let body = body.unwrap_or("");
                let mut req = format!(
                    "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
                    body.len()
                );
                if let Some(r) = run_header {
                    req.push_str(&format!("X-Run-Id: {r}\r\n"));
                }
                req.push_str("\r\n");
                req.push_str(body);
                stream.write_all(req.as_bytes()).unwrap();
                let mut raw = Vec::new();
                stream.read_to_end(&mut raw).unwrap();
                return parse(&raw);
            }
            Err(e) => {
                last_err = Some(e);
                std::thread::sleep(Duration::from_millis(20 + attempt * 5));
            }
        }
    }
    panic!("could not connect: {:?}", last_err);
}

fn parse(raw: &[u8]) -> Response {
    let text = String::from_utf8_lossy(raw);
    let (head, body) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
    let status = head
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let run_id_header = head
        .lines()
        .find(|l| l.to_ascii_lowercase().starts_with("x-run-id:"))
        .and_then(|l| l.split_once(':').map(|x| x.1))
        .map(|s| s.trim().to_string());
    Response {
        status,
        body: body.to_string(),
        run_id_header,
    }
}

fn json_get(addr: SocketAddr, path: &str, run: Option<&str>) -> (u16, serde_json::Value) {
    let r = request(addr, "GET", path, None, run);
    (
        r.status,
        serde_json::from_str(&r.body).unwrap_or(serde_json::json!({"parse_error": r.body})),
    )
}
fn json_post(
    addr: SocketAddr,
    path: &str,
    body: &str,
    run: Option<&str>,
) -> (u16, serde_json::Value) {
    let r = request(addr, "POST", path, Some(body), run);
    (
        r.status,
        serde_json::from_str(&r.body).unwrap_or(serde_json::json!({"parse_error": r.body})),
    )
}

/// Run a closure with a fresh in-process server.
fn with_server(f: impl FnOnce(SocketAddr)) {
    let (guard, _dir) = start_server();
    // Wait for the listener to accept (it already bound, so it's ready).
    f(guard.addr);
    drop(guard);
}

#[test]
fn health_and_run_id_propagation() {
    let mut log = RunRecorder::start("06-health-runid");
    with_server(|addr| {
        let r = request(addr, "GET", "/healthz", None, Some("run-fixed-123"));
        let body: serde_json::Value = serde_json::from_str(&r.body).unwrap();
        assert_eq!(r.status, 200);
        assert_eq!(body["ok"], true);
        assert_eq!(body["run_id"], "run-fixed-123");
        assert_eq!(r.run_id_header.as_deref(), Some("run-fixed-123"));
        assert!(body.get("bytes_on_disk").is_some());

        // No header -> server synthesizes one and echoes it in body and header.
        let r = request(addr, "GET", "/healthz", None, None);
        assert_eq!(r.status, 200);
        let synthesized = r.run_id_header.unwrap_or_default();
        assert!(synthesized.starts_with("run-"), "got {synthesized}");
        log.state("synthesized run id", synthesized);
    });
    log.finish(true);
}

#[test]
fn end_to_end_encode_append_decode_with_cross_check() {
    let mut log = RunRecorder::start("06-e2e");
    with_server(|addr| {
        // create stream
        let (st, body) = json_post(
            addr,
            "/v1/streams",
            r#"{"stream_id":"demo"}"#,
            Some("r-create"),
        );
        assert_eq!(st, 200);
        assert_eq!(body["stream_id"], "demo");

        // encode first block
        let b64 = lz77b::util_b64::encode(b"hello lz77 hello lz77 hello lz77");
        let payload = format!(r#"{{"data":"{}"}}"#, b64);
        let (st, body) = json_post(addr, "/v1/streams/demo/encode", &payload, Some("r-enc0"));
        assert_eq!(st, 200, "body={body}");
        assert_eq!(body["index"], 0);
        assert!(body["match_tokens"].as_u64().unwrap() >= 1);
        let block0 = body["block"].as_str().unwrap().to_string();

        // second block reusing dictionary
        let b64_1 = lz77b::util_b64::encode(b"hello lz77 again");
        let (st, body) = json_post(
            addr,
            "/v1/streams/demo/encode",
            &format!(r#"{{"data":"{}"}}"#, b64_1),
            Some("r-enc1"),
        );
        assert_eq!(st, 200, "body={body}");
        assert_eq!(body["index"], 1);

        // decode whole stream
        let (st, body) = json_get(addr, "/v1/streams/demo/decode", Some("r-dec"));
        assert_eq!(st, 200);
        let data = lz77b::util_b64::decode(body["data"].as_str().unwrap()).unwrap();
        assert_eq!(&data, b"hello lz77 hello lz77 hello lz77hello lz77 again");

        // stateless one-shot decode of block0 with reference cross-check
        let cross = format!(r#"{{"block":"{}","cross_check":true}}"#, block0);
        let (st, body) = json_post(addr, "/v1/decode", &cross, Some("r-cross"));
        assert_eq!(st, 200, "body={body}");
        assert_eq!(body["cross_check"]["reference_agrees"], true);
        log.state("cross-check body", body["cross_check"].clone());
    });
    log.finish(true);
}

#[test]
fn http_maps_all_four_error_categories() {
    let mut log = RunRecorder::start("06-error-mapping");
    with_server(|addr| {
        // Input: malformed JSON -> 400
        let (st, body) = json_post(
            addr,
            "/v1/encode-independent",
            "{not json",
            Some("r-badjson"),
        );
        assert_eq!(st, 400);
        assert_eq!(body["category"], "input");
        log.state("400 code", body["error_code"].as_str().unwrap_or(""));

        // Input: invalid base64 -> 400
        let (st, body) = json_post(
            addr,
            "/v1/encode-independent",
            r#"{"data":"@@@"}"#,
            Some("r-badb64"),
        );
        assert_eq!(st, 400);
        assert_eq!(body["category"], "input");

        // C2 regression: non-canonical padding ('=' mid-stream) is rejected at
        // the service boundary, not silently decoded to wrong bytes.
        let (st, body) = json_post(
            addr,
            "/v1/encode-independent",
            r#"{"data":"AAAAZg=A"}"#,
            Some("r-badpad"),
        );
        assert_eq!(st, 400, "body={body}");
        assert_eq!(body["category"], "input");
        // Nonzero trailing bits.
        let (st, _) = json_post(
            addr,
            "/v1/encode-independent",
            r#"{"data":"ZR=="}"#,
            Some("r-badbits"),
        );
        assert_eq!(st, 400);

        // State: append a dependent block to a stream with no predecessor.
        let bad_block = {
            let out = lz77b::core::encoder::encode_block(
                lz77b::core::format::FrameType::Dependent,
                1,
                b"some dictionary bytes 1234567890",
                b"x",
            )
            .unwrap();
            lz77b::util_b64::encode(&out.raw)
        };
        let (st, body) = json_post(
            addr,
            "/v1/streams/newstream/blocks",
            &format!(r#"{{"block":"{}"}}"#, bad_block),
            Some("r-state"),
        );
        assert_eq!(st, 409, "body={body}");
        assert_eq!(body["category"], "state");

        // Resource: stateless decode of a header-only output bomb.
        let bomb = {
            let mut raw = Vec::new();
            lz77b::core::format::BlockHeader {
                frame_type: lz77b::core::format::FrameType::Independent,
                index: 0,
                prev_digest: 0,
                payload_crc: 0,
                decompressed_len: 4u64 * 1024 * 1024 * 1024,
            }
            .encode(&mut raw);
            raw.push(0x02);
            lz77b::util_b64::encode(&raw)
        };
        let (st, body) = json_post(
            addr,
            "/v1/decode",
            &format!(r#"{{"block":"{}"}}"#, bomb),
            Some("r-res"),
        );
        assert_eq!(st, 413, "body={body}");
        assert_eq!(body["category"], "resource");

        // A missing stream is a NotFound inside the state category but must be
        // surfaced with REST status 404 (body still records category=state).
        let (st, body) = json_get(addr, "/v1/streams/does-not-exist/decode", Some("r-404"));
        assert_eq!(st, 404, "body={body}");
        assert_eq!(body["error_code"], "not_found");
        assert_eq!(body["category"], "state");

        log.note("compute(500) corresponds to local I/O failure; the error-contract unit tests cover Code::Io mapping, and store tests exercise real fs failures.");
    });
    log.finish(true);
}

#[test]
fn http_rejects_oversized_body() {
    let mut log = RunRecorder::start("06-body-limit");
    with_server(|addr| {
        let huge = format!("\"data\":\"{}\"", "A".repeat(2_100_000));
        let r = request(
            addr,
            "POST",
            "/v1/encode-independent",
            Some(&huge),
            Some("r-huge"),
        );
        log.state("status", r.status);
        assert!(
            r.status == 413 || r.status == 400,
            "axum body limit expected, got {}",
            r.status
        );
    });
    log.finish(true);
}
