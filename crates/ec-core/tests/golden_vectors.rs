//! Byte-for-byte comparison against the independent Python oracle:
//! encoder output, position-aware shard digests, and manifest covered TLV.

mod common;

use ec_core::reed_solomon::encode;
use ec_core::verify::digest_shard;
use ec_core::CodecConfig;

#[test]
fn encoder_output_matches_python_oracle_for_every_golden_case() {
    for g in common::golden_cases() {
        let cfg = CodecConfig::new(g.k as u16, g.m as u16).unwrap();
        let enc = encode(&cfg, &g.original);
        assert_eq!(enc.shard_len, g.shard_len, "case {}", g.case_id);
        assert_eq!(enc.shards.len(), g.k + g.m);
        for i in 0..g.shards.len() {
            assert_eq!(
                enc.shards[i], g.shards[i],
                "case {} shard {i} differs from Python oracle bytes",
                g.case_id
            );
        }
    }
}

#[test]
fn shard_digests_match_oracle_hex_strings() {
    for g in common::golden_cases() {
        for (i, expected_hex) in g.shard_digests_hex.iter().enumerate() {
            let got = digest_shard(i as u16, &g.shards[i]);
            assert_eq!(
                hex::encode(&got),
                *expected_hex,
                "case {} shard {i} digest differs from oracle",
                g.case_id
            );
        }
    }
}

#[test]
fn field_and_cauchy_known_answer_vectors_match_fixtures() {
    use ec_gf::{inv, mul, mul_by_x};
    let fx = common::load_fixtures();
    let kat = &fx["gf_kat"];
    let hx = |s: &str| u8::from_str_radix(s.trim_start_matches("0x"), 16).unwrap();
    assert_eq!(
        mul(0x57, 0x83),
        hx(kat["mul_0x57_0x83"].as_str().unwrap())
    );
    assert_eq!(inv(0x53), hx(kat["inv_0x53"].as_str().unwrap()));
    assert_eq!(mul_by_x(128), hx(kat["mulbyx_128"].as_str().unwrap()));

    let row: Vec<String> = fx["cauchy_known"]["k3_m2_row_p0"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| {
            let b = hx(v.as_str().unwrap());
            format!("{b:02x}")
        })
        .collect();
    assert_eq!(row, vec!["f6", "8d", "01"]);
}

#[test]
fn padding_lengths_are_exact_per_golden_manifest() {
    for g in common::golden_cases() {
        let capacity = g.k * g.shard_len;
        assert_eq!(
            capacity - g.original.len(),
            g.pad_len as usize,
            "case {}",
            g.case_id
        );
    }
}
