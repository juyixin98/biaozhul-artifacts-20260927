//! Shared helpers for integration tests: isolated state, HTTP calls and
//! assertions on the error contract. Nothing here uses the FM index itself
//! to generate expected answers — expected positions come from the
//! independent naive scan or from literals.

#![allow(dead_code)]

use std::path::PathBuf;

use axum::body::Body;
use base64::Engine;
use fm_index_svc::config::Config;
use fm_index_svc::service::{AppState, oneshot};
use http_body_util::BodyExt;
use serde_json::Value;

pub fn b64<T: AsRef<[u8]>>(b: T) -> String {
    base64::engine::general_purpose::STANDARD.encode(b.as_ref())
}

/// A self-contained service instance rooted in a fresh temp directory.
pub struct TestEnv {
    pub state: AppState,
    pub dir: tempfile::TempDir,
    pub log_dir: PathBuf,
}

impl TestEnv {
    pub fn new(max_text_bytes: usize, default_k: u32) -> Self {
        let dir = tempfile::tempdir().expect("tempdir");
        let log_dir = dir.path().join("logs");
        let cfg = Config {
            data_dir: dir.path().join("data"),
            bind: "127.0.0.1:0".into(),
            max_text_bytes,
            default_sample_interval: default_k,
            log_dir: Some(log_dir.clone()),
            rust_log: "error".into(),
        };
        let state = AppState::new(cfg).expect("state");
        Self {
            state,
            dir,
            log_dir,
        }
    }

    pub async fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<Value>,
    ) -> (u16, Value, String) {
        let req = axum::http::Request::builder()
            .method(method)
            .uri(path)
            .header("content-type", "application/json");
        let req = match body {
            Some(v) => req
                .body(Body::from(serde_json::to_vec(&v).unwrap()))
                .unwrap(),
            None => req.body(Body::empty()).unwrap(),
        };
        let resp = oneshot(self.state.clone(), req).await;
        let status = resp.status().as_u16();
        let run_id = resp
            .headers()
            .get("x-run-id")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        let bytes = resp.into_body().collect().await.expect("body").to_bytes();
        let json = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).expect("json body")
        };
        (status, json, run_id)
    }

    pub async fn request_raw(&self, method: &str, path: &str, raw: &str) -> (u16, Value, String) {
        let req = axum::http::Request::builder()
            .method(method)
            .uri(path)
            .header("content-type", "application/json")
            .body(Body::from(raw.to_string()))
            .unwrap();
        let resp = oneshot(self.state.clone(), req).await;
        let status = resp.status().as_u16();
        let run_id = resp
            .headers()
            .get("x-run-id")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        let bytes = resp.into_body().collect().await.expect("body").to_bytes();
        let json = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, json, run_id)
    }

    pub async fn create(&self, name: &str, text: &[u8], k: Option<u32>) -> (u16, Value) {
        let mut body = serde_json::json!({ "name": name, "text": b64(text) });
        if let Some(k) = k {
            body["sample_interval"] = Value::from(k);
        }
        let (s, v, _) = self.request("POST", "/indexes", Some(body)).await;
        (s, v)
    }

    pub async fn search_b64(&self, name: &str, patterns: &[impl AsRef<str>]) -> (u16, Value) {
        let patterns: Vec<String> = patterns.iter().map(|s| s.as_ref().to_string()).collect();
        let (s, v, _) = self
            .request(
                "POST",
                &format!("/indexes/{name}/search"),
                Some(serde_json::json!({ "patterns": patterns })),
            )
            .await;
        (s, v)
    }

    pub async fn verify_b64(&self, name: &str, patterns: &[impl AsRef<str>]) -> (u16, Value) {
        let patterns: Vec<String> = patterns.iter().map(|s| s.as_ref().to_string()).collect();
        let (s, v, _) = self
            .request(
                "POST",
                &format!("/indexes/{name}/verify"),
                Some(serde_json::json!({ "patterns": patterns })),
            )
            .await;
        (s, v)
    }

    pub fn request_log(&self) -> String {
        std::fs::read_to_string(self.log_dir.join("requests.jsonl")).unwrap_or_default()
    }
}

/// Independent scan used only by *tests* to build expected answers.
pub fn expected_positions(text: &[u8], pattern: &[u8]) -> Vec<u64> {
    fm_index_svc::reference::naive_scan(text, pattern)
}

pub fn positions_of(v: &Value, pattern_index: usize) -> Vec<u64> {
    v["results"][pattern_index]["positions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_u64().unwrap())
        .collect()
}
