//! Position-sampling tests: sparse vs. dense sample intervals must give
//! identical localized positions, including after a fresh catalog reload
//! (i.e. from disk, not from the in-memory build).

mod common;

use common::*;
use fm_index_svc::persistence::Catalog;
use fm_index_svc::reference::naive_scan;

#[tokio::test]
async fn sparse_and_dense_sampling_agree_with_scan() {
    let text: Vec<u8> = b"the quick brown fox jumps over the lazy dog; ".repeat(20);
    let patterns: Vec<Vec<u8>> = vec![
        b"the".to_vec(),
        b"dog".to_vec(),
        b"o".to_vec(),
        b"quick brown".to_vec(),
        b"zzz".to_vec(),
        b"; the lazy".to_vec(),
        vec![0u8], // absent byte
    ];
    for k in [1u32, 2, 5, 13, 37, 257] {
        let env = TestEnv::new(1 << 20, k);
        let (s, err) = env.create("sampled", &text, Some(k)).await;
        assert_eq!(s, 201, "k={k}: {err}");
        let refs: Vec<String> = patterns.iter().map(b64).collect();
        let refs_s: Vec<&str> = refs.iter().map(String::as_str).collect();
        let (s, res) = env.search_b64("sampled", &refs_s).await;
        assert_eq!(s, 200);
        for (i, p) in patterns.iter().enumerate() {
            assert_eq!(
                positions_of(&res, i),
                naive_scan(&text, p),
                "k={k}, pattern {p:?}"
            );
        }
    }
}

#[test]
fn localization_after_reload_from_disk_matches_scan() {
    let dir = tempfile::tempdir().unwrap();
    let text: Vec<u8> = b"mississippi-".repeat(30);
    {
        let mut cat = Catalog::open(dir.path()).unwrap();
        cat.create("reload", text.clone(), 11).unwrap();
    }
    // New catalog instance: no in-memory state survives.
    let cat = Catalog::open(dir.path()).unwrap();
    let idx = cat.open_index("reload").unwrap();
    for pat in [&b"ssi"[..], b"miss", b"i", b"ppi-miss", b"eeee"] {
        let out = idx.search(pat);
        assert_eq!(out.positions, naive_scan(&text, pat), "pat {pat:?}");
        // interval size always equals the number of localized positions
        assert_eq!(out.count as usize, out.positions.len());
    }
}

#[test]
fn every_row_localizes_to_a_unique_position_for_all_sample_rates() {
    // The localization map rows -> text positions must be a bijection onto
    // 0..=text_len for every K. This directly exercises LF termination.
    let texts: [&[u8]; 4] = [
        b"z",
        b"aaaa",
        b"\x00\x00\xff\x00",
        b"abracadabra-abracadabra",
    ];
    for text in texts {
        for k in 1..=8u32 {
            let idx = fm_index_svc::fm::FmIndex::build(text.to_vec(), k).unwrap();
            let mut got: Vec<u64> = (0..idx.coded_len()).map(|r| idx.locate_row(r)).collect();
            got.sort_unstable();
            assert_eq!(
                got,
                (0..idx.coded_len()).collect::<Vec<_>>(),
                "text {text:?} k {k}"
            );
        }
    }
}
