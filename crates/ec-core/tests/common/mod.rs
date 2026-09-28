//! Shared test helpers: load the Python-generated golden fixtures.
//!
//! IMPORTANT: expected bytes in these tests come from
//! `tests/reference/oracle.py` — an independent Python implementation — not
//! from the crate under test. The fixtures JSON is loaded at test time; run
//! `python3 tests/reference/oracle.py emit --out tests/reference/fixtures.json`
//! (committed; the integration tests would fail loudly if it is stale or
//! missing).

#![allow(dead_code)]

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use serde_json::Value;
use std::path::PathBuf;

pub fn fixtures_path() -> PathBuf {
    // CARGO_MANIFEST_DIR = .../crates/ec-core ; fixtures live at repo root.
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("../../tests/reference/fixtures.json");
    p
}

pub fn load_fixtures() -> Value {
    let path = fixtures_path();
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "cannot read golden fixtures at {}: {e}\n\
             regenerate with: python3 tests/reference/oracle.py emit --out tests/reference/fixtures.json",
            path.display()
        )
    });
    serde_json::from_str(&text).expect("fixtures.json must be valid JSON")
}

pub struct GoldenCase {
    pub case_id: String,
    pub k: usize,
    pub m: usize,
    pub original: Vec<u8>,
    pub shard_len: usize,
    pub pad_len: u64,
    pub shards: Vec<Vec<u8>>,
    pub shard_digests_hex: Vec<String>,
    pub covered_tlv_hex: String,
    pub manifest: Value,
}

pub fn golden_cases() -> Vec<GoldenCase> {
    let fx = load_fixtures();
    fx["cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| GoldenCase {
            case_id: c["case_id"].as_str().unwrap().to_string(),
            k: c["k"].as_u64().unwrap() as usize,
            m: c["m"].as_u64().unwrap() as usize,
            original: B64.decode(c["original_b64"].as_str().unwrap()).unwrap(),
            shard_len: c["shard_len"].as_u64().unwrap() as usize,
            pad_len: c["pad_len"].as_u64().unwrap(),
            shards: c["shards_b64"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| B64.decode(v.as_str().unwrap()).unwrap())
                .collect(),
            shard_digests_hex: c["shard_digests_hex"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect(),
            covered_tlv_hex: c["covered_tlv_hex"].as_str().unwrap().to_string(),
            manifest: c["manifest"].clone(),
        })
        .collect()
}

pub fn case_by_id(id: &str) -> GoldenCase {
    golden_cases()
        .into_iter()
        .find(|c| c.case_id == id)
        .unwrap_or_else(|| panic!("golden case {id} not found"))
}

pub fn b64(s: &str) -> Vec<u8> {
    B64.decode(s).unwrap()
}
