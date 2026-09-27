//! Golden-vector cross-validation.
//!
//! Fixtures are produced by `tools/gen_golden.py`, an independent Python
//! implementation, and are committed under `fixtures/`. Each roundtrip vector
//! is decoded by BOTH decoders (production `huff-core` + independent trie
//! walker in `huff-it::indie`) and compared to the Python-provided original.
//! Each negative vector must fail with the exact documented error code in
//! BOTH decoders, and the multi-step validation report must name it.

use std::fs;
use std::path::{Path, PathBuf};

use huff_core::container::decode_container;
use huff_core::report;
use huff_it::indie;
use serde_json::Value;

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures")
}

fn manifest() -> Value {
    let text = fs::read_to_string(fixtures_dir().join("golden.json"))
        .expect("golden.json missing; run `python3 tools/gen_golden.py`");
    serde_json::from_str(&text).expect("golden.json must be valid JSON")
}

#[test]
fn golden_roundtrip_vectors_match_both_decoders() {
    let manifest = manifest();
    let cases = manifest["cases"].as_array().unwrap();
    let roundtrip: Vec<&Value> = cases.iter().filter(|c| c["kind"] == "roundtrip").collect();
    assert!(roundtrip.len() >= 7, "expected the documented corpus, got {}", roundtrip.len());

    for case in roundtrip {
        let name = case["name"].as_str().unwrap();
        let blob = fs::read(fixtures_dir().join(case["file"].as_str().unwrap()))
            .unwrap_or_else(|e| panic!("missing fixture {name}: {e}"));
        let expected = base64_decode(case["original_b64"].as_str().unwrap());

        // 1. Production decoder.
        let production = decode_container(&blob)
            .unwrap_or_else(|e| panic!("[{name}] production decoder failed: {e:?}"));
        assert_eq!(production, expected, "[{name}] production output mismatch");
        assert_eq!(
            production.len() as u64,
            case["original_len"].as_u64().unwrap(),
            "[{name}] original_len"
        );

        // 2. Independent trie-walk decoder (different language of origin for
        //    the fixture, different algorithm here).
        let independent = indie::decode(&blob)
            .unwrap_or_else(|e| panic!("[{name}] independent decoder failed: {e:?}"));
        assert_eq!(independent, expected, "[{name}] independent output mismatch");
    }
}

#[test]
fn golden_negative_vectors_fail_with_exact_error_codes() {
    let manifest = manifest();
    let negatives: Vec<&Value> = manifest["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["kind"] == "negative")
        .collect();
    assert_eq!(negatives.len(), 4, "the four mandated negative cases");

    for case in negatives {
        let name = case["name"].as_str().unwrap();
        let want = case["expected_error"].as_str().unwrap();
        let blob = fs::read(fixtures_dir().join(case["file"].as_str().unwrap())).unwrap();

        // Production decoder: exact failure category, never success.
        let prod_err = decode_container(&blob)
            .err()
            .unwrap_or_else(|| panic!("[{name}] production decoder unexpectedly succeeded"));
        assert_eq!(
            prod_err.code(),
            want,
            "[{name}] production: expected {want}, got {prod_err:?}"
        );

        // Independent decoder: same failure category.
        let indie_err = indie::decode(&blob)
            .err()
            .unwrap_or_else(|| panic!("[{name}] independent decoder unexpectedly succeeded"));
        assert_eq!(
            indie_err.code(),
            want,
            "[{name}] independent: expected {want}, got {indie_err:?}"
        );

        // Validation report must record the failure, not collapse it.
        let report = report::validate(&blob);
        assert!(!report.ok, "[{name}] report must be false");
        assert_eq!(
            report.first_error.as_deref(),
            Some(want),
            "[{name}] report first_error"
        );
        assert!(report.checks.iter().any(|c| c.verdict == "fail"));
    }
}

#[test]
fn unknown_version_is_refused_not_guessed() {
    // Explicitly assert the normative version gate, independent of fixtures.
    let manifest = manifest();
    let case = manifest["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == "unknown_version")
        .unwrap();
    let blob = fs::read(fixtures_dir().join(case["file"].as_str().unwrap())).unwrap();
    assert_eq!(decode_container(&blob).err().map(|e| e.code()), Some("UNKNOWN_VERSION"));
    assert_eq!(indie::decode(&blob).err().map(|e| e.code()), Some("UNKNOWN_VERSION"));
    assert_eq!(blob[4], 99, "fixture really is an unknown version");
}

// Small local base64 decoder so test support needs no extra crate.
mod base64_decode {
    pub fn call(s: &str) -> Vec<u8> {
        let table = |c: u8| -> Option<u8> {
            match c {
                b'A'..=b'Z' => Some(c - b'A'),
                b'a'..=b'z' => Some(c - b'a' + 26),
                b'0'..=b'9' => Some(c - b'0' + 52),
                b'+' => Some(62),
                b'/' => Some(63),
                _ => None,
            }
        };
        let bytes: Vec<u8> = s.bytes().filter(|b| !matches!(b, b'\n' | b'\r' | b'=')).collect();
        let mut out = Vec::new();
        for chunk in bytes.chunks(4) {
            let mut buf = 0u32;
            let mut bits = 0;
            for &b in chunk {
                buf = (buf << 6) | table(b).unwrap() as u32;
                bits += 6;
            }
            while bits >= 8 {
                bits -= 8;
                out.push((buf >> bits) as u8);
                buf &= (1 << bits) - 1;
            }
        }
        out
    }
}
use base64_decode::call as base64_decode;
