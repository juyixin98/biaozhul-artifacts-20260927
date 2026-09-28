//! Build -> save -> load consistency and on-disk format validation.

use std::path::PathBuf;

use wavelet_matrix_service::error::WmError;
use wavelet_matrix_service::format;
use wavelet_matrix_service::index::WmIndex;
use wavelet_matrix_service::store::IndexStore;

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "wm-persist-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&p).unwrap();
        TempDir(p)
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn save_load_roundtrip_answers_identically() {
    let data = [
        5i64,
        -3,
        7,
        7,
        0,
        -3,
        42,
        1,
        i64::MIN,
        i64::MAX,
        i64::MIN,
        -1,
    ];
    let original = WmIndex::build(&data).unwrap();

    let dir = TempDir::new();
    let store = IndexStore::new(&dir.0).unwrap();
    let path = store.save("demo", &original).unwrap();
    assert!(path.ends_with("demo.wmx"));

    let loaded = store.load("demo").unwrap();
    assert_eq!(original, loaded);

    // Structural metadata
    assert_eq!(loaded.len(), data.len());
    assert_eq!(loaded.distinct(), 9);
    assert_eq!(loaded.height(), original.height());

    // Every window/k and a spread of bounds must agree.
    let n = data.len();
    for l in 0..n {
        for r in (l + 1)..=n {
            for k in 0..(r - l) {
                assert_eq!(
                    loaded.kth_smallest(l, r, k).unwrap(),
                    original.kth_smallest(l, r, k).unwrap()
                );
            }
            for bound in [i64::MIN, -3, 0, 7, 42, i64::MAX] {
                assert_eq!(
                    loaded.count_lt(l, r, bound).unwrap(),
                    original.count_lt(l, r, bound).unwrap()
                );
                assert_eq!(
                    loaded.predecessor(l, r, bound).unwrap(),
                    original.predecessor(l, r, bound).unwrap()
                );
                assert_eq!(
                    loaded.successor(l, r, bound).unwrap(),
                    original.successor(l, r, bound).unwrap()
                );
            }
        }
    }

    // Re-encoding the loaded index must reproduce the bytes exactly:
    // the format is canonical and the rank tables are derived.
    let raw = std::fs::read(&path).unwrap();
    assert_eq!(raw, format::encode(&loaded));
}

#[test]
fn list_finds_only_valid_index_files() {
    let dir = TempDir::new();
    let store = IndexStore::new(&dir.0).unwrap();
    store
        .save("alpha-1", &WmIndex::build(&[1, 2, 3]).unwrap())
        .unwrap();
    store
        .save("beta_2", &WmIndex::build(&[-1, 0, 1]).unwrap())
        .unwrap();
    std::fs::write(dir.0.join("not-an-index.txt"), b"hello").unwrap();
    std::fs::write(dir.0.join(".hidden.wmx"), b"hello").unwrap();

    let mut names = store.list().unwrap();
    names.sort();
    assert_eq!(names, vec!["alpha-1".to_string(), "beta_2".to_string()]);
}

#[test]
fn corrupt_truncated_and_unknown_version_files_fail() {
    let dir = TempDir::new();
    let store = IndexStore::new(&dir.0).unwrap();
    let idx = WmIndex::build(&[1, -1, 0, 3, i64::MIN]).unwrap();
    let bytes = format::encode(&idx);

    // truncated record
    assert!(matches!(
        format::decode(&bytes[..bytes.len() - 2]),
        Err(WmError::CorruptFormat(_))
    ));

    // flipped payload byte -> checksum mismatch
    let mut flipped = bytes.clone();
    flipped[16] ^= 0x01;
    assert!(matches!(
        format::decode(&flipped),
        Err(WmError::CorruptFormat(_))
    ));

    // bad magic
    let mut bad_magic = bytes.clone();
    bad_magic[0] = b'X';
    assert!(matches!(
        format::decode(&bad_magic),
        Err(WmError::CorruptFormat(_))
    ));

    // unknown version
    let mut bumped = bytes.clone();
    bumped[4..8].copy_from_slice(&999u32.to_le_bytes());
    assert_eq!(
        format::decode(&bumped).unwrap_err(),
        WmError::UnsupportedVersion(999)
    );

    // garbage bytes
    assert!(matches!(
        format::decode(b"not a wavelet matrix file at all"),
        Err(WmError::CorruptFormat(_))
    ));

    // a corrupt file persisted through the store surfaces the format error
    std::fs::write(store.dir().join("broken.wmx"), &bumped).unwrap();
    assert!(matches!(
        store.load("broken"),
        Err(WmError::UnsupportedVersion(999))
    ));
}

#[test]
fn missing_index_and_invalid_names_have_distinct_errors() {
    let dir = TempDir::new();
    let store = IndexStore::new(&dir.0).unwrap();
    assert_eq!(
        store.load("ghost").unwrap_err(),
        WmError::IndexNotFound("ghost".to_string())
    );

    for bad in ["", "a/b", "../escape", ".dot", "a b", "x?", "日本語"] {
        assert!(
            matches!(
                store.save(bad, &WmIndex::build(&[1]).unwrap()),
                Err(WmError::InvalidIndexName(_))
            ),
            "name {bad:?} should be rejected"
        );
    }
    for good in ["a", "A1", "idx-2_v3"] {
        assert!(IndexStore::validate_name(good).is_ok());
    }
}
