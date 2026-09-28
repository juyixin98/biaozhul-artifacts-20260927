//! End-to-end checks over the bundled synthetic fixtures in `samples/`.
//!
//! Fixtures are generated deterministically by `fm-make-fixtures` and checked
//! into the repo, so these tests run offline. Expected positions are computed
//! by the independent naive scan of the *decoded* text — the fixture file
//! only supplies inputs, never expected answers.

mod common;

use base64::Engine;
use common::*;
use serde::Deserialize;

#[derive(Deserialize)]
struct Fixture {
    #[allow(dead_code)]
    description: String,
    #[allow(dead_code)]
    encoding: String,
    text_b64: String,
    patterns_b64: Vec<String>,
}

fn load(name: &str) -> Fixture {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("samples")
        .join(name);
    let raw = std::fs::read(&path).unwrap_or_else(|e| {
        panic!(
            "missing {} (run `cargo run --bin fm-make-fixtures`): {e}",
            path.display()
        )
    });
    serde_json::from_slice(&raw).unwrap()
}

fn std_b64() -> &'static base64::engine::GeneralPurpose {
    &base64::engine::general_purpose::STANDARD
}

async fn run_fixture(file: &str, index: &str) {
    let fx = load(file);
    let text = std_b64().decode(&fx.text_b64).unwrap();
    let patterns: Vec<Vec<u8>> = fx
        .patterns_b64
        .iter()
        .map(|s| std_b64().decode(s).unwrap())
        .collect();

    for k in [1u32, 8, 32] {
        let env = TestEnv::new(8 << 20, k);
        let (s, err) = env.create(index, &text, Some(k)).await;
        assert_eq!(s, 201, "create {index} k={k}: {err}");

        let refs: Vec<&str> = fx.patterns_b64.iter().map(String::as_str).collect();
        let (s, res) = env.search_b64(index, &refs).await;
        assert_eq!(s, 200);
        for (i, p) in patterns.iter().enumerate() {
            assert_eq!(
                positions_of(&res, i),
                expected_positions(&text, p),
                "{file} k={k} pattern {i}: {:?}",
                p
            );
        }

        // /verify agrees and reports the text-level byte statistics.
        let (s, ver) = env.verify_b64(index, &refs).await;
        assert_eq!(s, 200);
        assert_eq!(ver["all_agree"], true);
        assert_eq!(ver["text_len"], text.len() as u64);
        let expected_zeros = text.iter().filter(|b| **b == 0).count() as u64;
        assert_eq!(ver["zero_bytes"], expected_zeros);
    }
}

#[tokio::test]
async fn fixture_repetitive() {
    run_fixture("repetitive.json", "rep_fx").await;
}

#[tokio::test]
async fn fixture_zeros() {
    run_fixture("zeros.json", "zero_fx").await;
}

#[tokio::test]
async fn fixture_mixed() {
    run_fixture("mixed.json", "mix_fx").await;
}

#[tokio::test]
async fn fixture_zero_byte_exact_positions() {
    // Concrete assertions on the zeros fixture, not just scan equality.
    let fx = load("zeros.json");
    let text = std_b64().decode(&fx.text_b64).unwrap();
    assert_eq!(text.len(), 600);
    assert_eq!(text[0], 0xFF);
    assert_eq!(text[599], 0xFF);

    let env = TestEnv::new(8 << 20, 16);
    env.create("z", &text, Some(16)).await;
    // patterns order in the generator: [0], [0,0], [0,1,0], [FF], [FF,0], [2]
    let (s, res) = env
        .search_b64(
            "z",
            &fx.patterns_b64
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>(),
        )
        .await;
    assert_eq!(s, 200);
    assert_eq!(res["results"][3]["count"], 2); // exactly two 0xFF
    assert_eq!(positions_of(&res, 3), vec![0, 599]);
    assert_eq!(positions_of(&res, 5), Vec::<u64>::new()); // byte 0x02 absent
}
