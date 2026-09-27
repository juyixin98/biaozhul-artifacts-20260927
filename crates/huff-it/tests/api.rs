//! End-to-end HTTP tests against an in-process Axum server on an ephemeral
//! port. The server runs on its own current-thread Tokio runtime in a worker
//! thread; the test driver uses a plain blocking TCP client.

use std::fs;
use std::path::PathBuf;
use std::sync::mpsc;
use std::thread;

use huff_api::server::spawn_ephemeral;
use huff_it::http;
use serde_json::Value;

struct TestServer {
    addr: String,
    _dir: PathBuf,
}

fn spawn() -> TestServer {
    let dir = std::env::temp_dir().join(format!(
        "huff-api-it-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir_all(&dir).unwrap();
    let (tx, rx) = mpsc::channel();
    let dir_for_server = dir.clone();
    thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            let addr = spawn_ephemeral(dir_for_server, 4096, 1 << 20).await.unwrap();
            tx.send(addr.to_string()).unwrap();
            // Park: keep the runtime alive for the test's lifetime.
            futures_park().await;
        });
    });
    let addr = rx.recv().unwrap();
    TestServer { addr, _dir: dir }
}

async fn futures_park() {
    // Park until the process ends; detached tasks/connections still run.
    std::future::pending::<()>().await;
}

#[test]
fn health_reports_service_and_version() {
    let s = spawn();
    let resp = http::request(&s.addr, "GET", "/healthz", b"", &[]).unwrap();
    assert_eq!(resp.status, 200);
    let body: Value = serde_json::from_slice(&resp.body).unwrap();
    assert_eq!(body["ok"], true);
    assert_eq!(body["service"], "huff-canonical");
    assert_eq!(body["format_version"], 1);
}

#[test]
fn encode_then_decode_roundtrip_over_http() {
    let s = spawn();
    let input = b"http roundtrip payload, repeated ".repeat(50);

    let enc = http::request(&s.addr, "POST", "/v1/encode", &input, &[]).unwrap();
    assert_eq!(enc.status, 200, "encode body: {:?}", String::from_utf8_lossy(&enc.body));
    assert_eq!(enc.header("content-type"), Some("application/octet-stream"));
    let id = enc.header("x-artifact-id").expect("artifact id header").to_string();
    assert_eq!(id.len(), 64, "content id is a SHA-256 hex string");

    let dec = http::request(&s.addr, "POST", "/v1/decode", &enc.body, &[]).unwrap();
    assert_eq!(dec.status, 200);
    assert_eq!(dec.body, input);
    assert_eq!(
        dec.header("x-original-length").and_then(|v| v.parse::<usize>().ok()),
        Some(input.len())
    );
}

#[test]
fn empty_input_roundtrips_over_http() {
    let s = spawn();
    let enc = http::request(&s.addr, "POST", "/v1/encode", b"", &[]).unwrap();
    assert_eq!(
        enc.status,
        200,
        "empty encode failed: {}",
        String::from_utf8_lossy(&enc.body)
    );
    let dec = http::request(&s.addr, "POST", "/v1/decode", &enc.body, &[]).unwrap();
    assert_eq!(dec.status, 200);
    assert!(dec.body.is_empty());
}

#[test]
fn validate_ok_then_unknown_version_failure() {
    let s = spawn();
    let enc = http::request(&s.addr, "POST", "/v1/encode", b"validation corpus", &[]).unwrap();
    assert_eq!(enc.status, 200);

    let ok = http::request(&s.addr, "POST", "/v1/validate", &enc.body, &[]).unwrap();
    assert_eq!(ok.status, 200);
    let report: Value = serde_json::from_slice(&ok.body).unwrap();
    assert_eq!(report["ok"], true);
    assert!(report["checks"].as_array().unwrap().iter().all(|c| c["verdict"] == "pass"));

    // Unknown version: 422 + exact error code, never success.
    let mut bad = enc.body.clone();
    bad[4] = 99;
    let crc = crc32(&bad[..28]);
    bad[28..32].copy_from_slice(&crc.to_le_bytes());
    let fail = http::request(&s.addr, "POST", "/v1/validate", &bad, &[]).unwrap();
    assert_eq!(fail.status, 422);
    let report: Value = serde_json::from_slice(&fail.body).unwrap();
    assert_eq!(report["ok"], false);
    assert_eq!(report["first_error"], "UNKNOWN_VERSION");
}

#[test]
fn decode_rejects_garbage_with_error_code() {
    let s = spawn();
    // Truncated header: distinct category from a wrong magic.
    let short = http::request(&s.addr, "POST", "/v1/decode", b"not a container", &[]).unwrap();
    assert_eq!(short.status, 400);
    let body: Value = serde_json::from_slice(&short.body).unwrap();
    assert_eq!(body["error_code"], "HEADER_TRUNCATED");
    assert!(body["request_id"].as_str().unwrap().len() >= 16);

    // Full-length header with the wrong magic (422: unprocessable entity).
    let long = http::request(&s.addr, "POST", "/v1/decode", b"X".repeat(64).as_slice(), &[]).unwrap();
    assert_eq!(long.status, 422);
    let body: Value = serde_json::from_slice(&long.body).unwrap();
    assert_eq!(body["ok"], false);
    assert_eq!(body["error_code"], "BAD_MAGIC");
}

#[test]
fn artifacts_are_persisted_and_fetchable() {
    let s = spawn();
    let input = b"persist me across the artifact index";
    let enc = http::request(
        &s.addr,
        "POST",
        "/v1/encode",
        input,
        &[("x-label", "api-it")],
    )
    .unwrap();
    let id = enc.header("x-artifact-id").unwrap().to_string();

    let listed = http::request(&s.addr, "GET", "/v1/artifacts", b"", &[]).unwrap();
    assert_eq!(listed.status, 200);
    let arr: Value = serde_json::from_slice(&listed.body).unwrap();
    assert!(arr.as_array().unwrap().iter().any(|r| r["id"] == id && r["label"] == "api-it"));

    let got = http::request(&s.addr, "GET", &format!("/v1/artifacts/{id}"), b"", &[]).unwrap();
    assert_eq!(got.status, 200);
    assert_eq!(got.body, input);

    let missing = http::request(
        &s.addr,
        "GET",
        "/v1/artifacts/0000000000000000000000000000000000000000000000000000000000000000",
        b"",
        &[],
    )
    .unwrap();
    assert_eq!(missing.status, 404);
    let body: Value = serde_json::from_slice(&missing.body).unwrap();
    assert_eq!(body["error_code"], "ARTIFACT_NOT_FOUND");
}

#[test]
fn path_traversal_id_is_not_found_not_io_error() {
    let s = spawn();
    let resp = http::request(&s.addr, "GET", "/v1/artifacts/..%2F..%2Findex", b"", &[]).unwrap();
    // Axum may reject encoded slashes as 400; either way it must not 200.
    assert!(resp.status == 400 || resp.status == 404, "got {}", resp.status);
}

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0xEDB8_8320u32 & 0u32.wrapping_sub(crc & 1));
        }
    }
    crc ^ 0xFFFF_FFFF
}
