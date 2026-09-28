//! Independent end-to-end tests crossing the kernel, format and store
//! crates. Reference answers here are produced by plain sorting and linear
//! scans over the *original input file* — never by the kernel under test.

use std::fs;

use wm_core::WaveletMatrix;
use wm_format::{decode, encode};
use wm_store::IndexStore;

/// Read the checked-in fixture: one i64 per line, `#` comments allowed.
fn load_fixture(name: &str) -> Vec<i64> {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../fixtures")
        .join(name)
        .canonicalize()
        .expect("fixture path must canonicalize");
    let text = fs::read_to_string(&path).expect("fixture file must exist");
    text.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .map(|l| {
            l.parse::<i64>()
                .unwrap_or_else(|e| panic!("fixture {name}: invalid i64 {l:?}: {e}"))
        })
        .collect()
}

fn sorted(v: &[i64]) -> Vec<i64> {
    let mut s = v.to_vec();
    s.sort_unstable();
    s
}

#[test]
fn fixture_basic_matches_independent_sort_oracle() {
    let v = load_fixture("basic.txt");
    assert!(v.len() >= 10, "fixture should contain multiple values");
    let wm = WaveletMatrix::build(&v).unwrap();
    let s = sorted(&v);

    // Whole-range order statistics against the independently sorted vector.
    for (k, expected) in s.iter().enumerate() {
        assert_eq!(wm.quantile(0, v.len(), k as u64).unwrap(), *expected);
    }

    // Explicit sub-range [3, 9).
    let mut sub: Vec<i64> = v[3..9].to_vec();
    sub.sort_unstable();
    for (k, expected) in sub.iter().enumerate() {
        assert_eq!(wm.quantile(3, 9, k as u64).unwrap(), *expected);
    }

    // Independent linear counts.
    let lo: i64 = 0;
    let hi: i64 = 100;
    let expected = v.iter().filter(|&&x| lo <= x && x < hi).count() as u64;
    assert_eq!(wm.range_count(0, v.len(), lo, hi).unwrap(), expected);
}

#[test]
fn fixture_extremes_and_duplicates() {
    let v = load_fixture("extremes.txt");
    let wm = WaveletMatrix::build(&v).unwrap();
    let s = sorted(&v);

    assert_eq!(wm.quantile(0, v.len(), 0).unwrap(), i64::MIN);
    assert_eq!(
        wm.quantile(0, v.len(), v.len() as u64 - 1).unwrap(),
        i64::MAX
    );
    assert_eq!(s.first().copied(), Some(i64::MIN));
    assert_eq!(s.last().copied(), Some(i64::MAX));

    // Half-open [MIN, MAX) drops every MAX; inclusive keeps them.
    let maxes = v.iter().filter(|&&x| x == i64::MAX).count() as u64;
    assert_eq!(
        wm.range_count(0, v.len(), i64::MIN, i64::MAX).unwrap(),
        v.len() as u64 - maxes
    );
    assert_eq!(
        wm.range_count_inclusive(0, v.len(), i64::MIN, i64::MAX)
            .unwrap(),
        v.len() as u64
    );

    // All-same fixture exercises zero levels.
    let same = load_fixture("all_same.txt");
    let wm_same = WaveletMatrix::build(&same).unwrap();
    assert_eq!(wm_same.bit_len(), 0);
    assert_eq!(wm_same.distinct_count(), 1);
    assert_eq!(wm_same.quantile(0, same.len(), 0).unwrap(), -7);
    assert_eq!(
        wm_same.range_count(0, same.len(), -7, -6).unwrap(),
        same.len() as u64
    );
}

#[test]
fn build_save_load_byte_identical_and_queries_match() {
    let dir = std::env::temp_dir().join(format!(
        "wm-it-persist-{}-{}",
        std::process::id(),
        std::file!().replace('/', "_")
    ));
    let _ = fs::remove_dir_all(&dir);
    let store = IndexStore::open(&dir).unwrap();
    let v = load_fixture("basic.txt");

    let built = store.create_index("basic", &v, false).unwrap();
    let on_disk = fs::read(dir.join("basic.wmi")).unwrap();

    // encode(build) must be byte identical to the persisted file.
    assert_eq!(on_disk, encode(&built));

    // decode(persisted) must equal the in-memory index structurally.
    let decoded = decode(&on_disk).unwrap();
    assert_eq!(decoded, built);

    // store.load path gives the same object.
    let loaded = store.load("basic").unwrap();
    assert_eq!(loaded, built);

    // Answers survive the restart-style reload, checked by independent oracle.
    let s = sorted(&v);
    for k in (0..v.len()).step_by(3) {
        assert_eq!(loaded.quantile(0, v.len(), k as u64).unwrap(), s[k]);
    }

    // Reloading from a fresh store handle (simulates a new process).
    let reopened = IndexStore::open(&dir).unwrap();
    let reloaded = reopened.load("basic").unwrap();
    assert_eq!(reloaded, built);

    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn corrupted_persistence_is_rejected_with_category() {
    let dir = std::env::temp_dir().join(format!(
        "wm-it-corrupt-{}-{}",
        std::process::id(),
        std::line!()
    ));
    let _ = fs::remove_dir_all(&dir);
    let store = IndexStore::open(&dir).unwrap();
    store.create_index("x", &[1, 2, 3], false).unwrap();

    let path = dir.join("x.wmi");
    let mut bytes = fs::read(&path).unwrap();
    // Truncate inside the payload while keeping header+checksum slot.
    bytes.truncate(bytes.len() - 12);
    fs::write(&path, bytes).unwrap();

    let err = store.load("x").unwrap_err();
    let msg = err.to_string();
    assert!(
        msg.contains("length") || msg.contains("checksum") || msg.contains("truncated"),
        "rejection must name the corruption class, got: {msg}"
    );

    fs::remove_dir_all(&dir).unwrap();
}
