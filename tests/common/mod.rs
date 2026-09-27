//! Shared integration-test helpers.
#![allow(dead_code)]

use ec_service::storage::FileStore;
use ec_service::{build_state, AppState, Config};
use std::path::PathBuf;

pub fn unique_data_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "ec-service-test-{tag}-{}-{}",
        std::process::id(),
        uuid_suffix()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

fn uuid_suffix() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    N.fetch_add(1, Ordering::Relaxed)
}

pub async fn state_for(dir: &PathBuf, profiles: Vec<(u8, u8)>) -> AppState {
    let cfg = Config {
        data_dir: dir.to_string_lossy().to_string(),
        bind_addr: "127.0.0.1:0".into(),
        max_object_bytes: 1024 * 1024,
        allowed_profiles: profiles,
        started_unix: 0,
    };
    build_state(&cfg).await.unwrap()
}

pub fn store_for(dir: &PathBuf) -> FileStore {
    std::fs::create_dir_all(dir).unwrap();
    // synchronous test helper wrapping the async store
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(FileStore::new(dir.clone()))
        .unwrap()
}

/// Raw bytes of a shard file on disk.
pub fn read_shard_file(dir: &PathBuf, object_id: &str, index: u8) -> Option<Vec<u8>> {
    let p = dir.join(object_id).join(format!("shard-{index:03}.bin"));
    std::fs::read(p).ok()
}

pub fn manifest_path(dir: &PathBuf, object_id: &str) -> PathBuf {
    dir.join(object_id).join("manifest.json")
}

pub fn read_manifest_json(dir: &PathBuf, object_id: &str) -> serde_json::Value {
    serde_json::from_slice(&std::fs::read(manifest_path(dir, object_id)).unwrap()).unwrap()
}

/// Overwrite one shard file with corrupt bytes (same length, wrong content).
pub fn corrupt_shard_file(dir: &PathBuf, object_id: &str, index: u8) {
    let p = dir.join(object_id).join(format!("shard-{index:03}.bin"));
    let mut b = std::fs::read(&p).unwrap();
    for byte in b.iter_mut() {
        *byte ^= 0xff;
    }
    std::fs::write(&p, b).unwrap();
}

/// Flip a single byte inside one shard file.
pub fn flip_one_byte(dir: &PathBuf, object_id: &str, index: u8) {
    let p = dir.join(object_id).join(format!("shard-{index:03}.bin"));
    let mut b = std::fs::read(&p).unwrap();
    b[0] ^= 0x01;
    std::fs::write(&p, b).unwrap();
}

/// Truncate a shard file (wrong length).
pub fn truncate_shard_file(dir: &PathBuf, object_id: &str, index: u8) {
    let p = dir.join(object_id).join(format!("shard-{index:03}.bin"));
    let mut b = std::fs::read(&p).unwrap();
    b.pop();
    if b.is_empty() {
        b.push(0x77);
    }
    std::fs::write(&p, b).unwrap();
}

pub fn delete_shard_file(dir: &PathBuf, object_id: &str, index: u8) {
    let p = dir.join(object_id).join(format!("shard-{index:03}.bin"));
    let _ = std::fs::remove_file(p);
}

// ---------------------------------------------------- minimal HTTP client
//
// Async (tokio) so it can run on the current-thread test runtime alongside
// the spawned Axum server: awaiting yields the worker thread to the server
// instead of blocking it (a blocking std client here deadlocks).

pub struct HttpResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    pub fn json(&self) -> serde_json::Value {
        serde_json::from_slice(&self.body).unwrap_or_else(|e| {
            panic!(
                "response body was not JSON: {e}; body={}",
                String::from_utf8_lossy(&self.body)
            )
        })
    }
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

/// Issue one HTTP/1.1 request with `Connection: close` and read the full
/// response. Optional extra headers (e.g. x-request-id).
pub async fn http_request(
    addr: &str,
    method: &str,
    path: &str,
    body: &[u8],
    content_type: Option<&str>,
) -> std::io::Result<HttpResponse> {
    http_request_ex(addr, method, path, body, content_type, None).await
}

pub async fn http_request_ex(
    addr: &str,
    method: &str,
    path: &str,
    body: &[u8],
    content_type: Option<&str>,
    extra_headers: Option<&str>,
) -> std::io::Result<HttpResponse> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpStream;
    let mut stream = TcpStream::connect(addr).await?;
    let mut req = format!("{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n");
    if let Some(ct) = content_type {
        req.push_str(&format!("Content-Type: {ct}\r\n"));
    }
    if let Some(extra) = extra_headers {
        req.push_str(extra);
        if !extra.ends_with("\r\n") {
            req.push_str("\r\n");
        }
    }
    req.push_str(&format!("Content-Length: {}\r\n\r\n", body.len()));
    stream.write_all(req.as_bytes()).await?;
    stream.write_all(body).await?;
    stream.flush().await?;

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).await?;
    parse_response(&raw)
}

fn parse_response(raw: &[u8]) -> std::io::Result<HttpResponse> {
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "no header end"))?;
    let header_text = String::from_utf8_lossy(&raw[..split]);
    let mut lines = header_text.split("\r\n");
    let status_line = lines.next().unwrap_or("");
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let mut headers = Vec::new();
    let mut body = raw[split + 4..].to_vec();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            headers.push((k.trim().to_string(), v.trim().to_string()));
        }
    }
    // Honor a fixed content-length if present (defensive; Connection: close
    // already delimits the body here).
    if let Some((_, len)) = headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
    {
        if let Ok(n) = len.parse::<usize>() {
            body.truncate(n);
        }
    }
    Ok(HttpResponse {
        status,
        headers,
        body,
    })
}

/// Spawn the real Axum server on an ephemeral port; return its address.
pub async fn spawn_server(state: AppState) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    let app = ec_service::api::router(state);
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    // tiny readiness wait
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    addr
}
