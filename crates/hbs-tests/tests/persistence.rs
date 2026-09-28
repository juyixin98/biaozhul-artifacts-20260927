//! File-store persistence tests including on-disk corruption detection and
//! atomic replace semantics.

use std::fs;
use std::path::Path;

use hbs_core::HierBitmap;
use hbs_format::FormatError;
use hbs_store::FileStore;

fn temp_dir(tag: &str) -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!(
        "hbs-it-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir_all(&p).unwrap();
    p
}

fn denseish_set() -> HierBitmap {
    let mut v: Vec<u32> = (0..50_000).collect();
    v.push(u32::MAX);
    HierBitmap::from_sorted_unique(&v).unwrap()
}

#[test]
fn saved_file_is_rejected_after_byte_corruption() {
    let dir = temp_dir("corrupt");
    let store = FileStore::open(&dir, false).unwrap();
    let set = denseish_set();
    store.save("victim", &set).unwrap();

    let path: std::path::PathBuf = dir.join("victim.hbs");
    let mut bytes = fs::read(&path).unwrap();
    // Flip a bit in a bitmap payload (well past header + directory).
    let idx = bytes.len() - 100;
    bytes[idx] ^= 0x01;
    fs::write(&path, bytes).unwrap();

    let err = store.load("victim").unwrap_err();
    match err {
        hbs_store::StoreError::Corrupt(FormatError::ChecksumMismatch { .. }) => {}
        other => panic!("expected ChecksumMismatch, got {other}"),
    }
    // verify() reports the same category, and classify it explicitly.
    let verr = store.verify("victim").unwrap_err();
    assert!(matches!(
        verr,
        hbs_store::StoreError::Corrupt(FormatError::ChecksumMismatch { .. })
    ));
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn truncated_file_is_rejected() {
    let dir = temp_dir("trunc");
    let store = FileStore::open(&dir, false).unwrap();
    store.save("v", &denseish_set()).unwrap();
    let path = dir.join("v.hbs");
    let bytes = fs::read(&path).unwrap();
    fs::write(&path, &bytes[..bytes.len() / 2]).unwrap();
    assert!(matches!(
        store.load("v"),
        Err(hbs_store::StoreError::Corrupt(FormatError::Truncated))
    ));
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn foreign_file_with_bad_magic_is_rejected() {
    let dir = temp_dir("magic");
    let store = FileStore::open(&dir, false).unwrap();
    fs::write(
        dir.join("fake.hbs"),
        b"not an hbs file at all, padded.........",
    )
    .unwrap();
    let err = store.load("fake").unwrap_err();
    assert!(matches!(
        err,
        hbs_store::StoreError::Corrupt(FormatError::BadMagic)
    ));
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn atomic_replace_never_leaves_temp_files() {
    let dir = temp_dir("atomic");
    let store = FileStore::open(&dir, true).unwrap();
    store.save("s", &denseish_set()).unwrap();
    let mut smaller = HierBitmap::new();
    smaller.insert(1);
    store.save("s", &smaller).unwrap();
    assert_eq!(store.load("s").unwrap(), smaller);

    // No .tmp artifacts remain.
    let leftovers: Vec<String> = fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
        .filter(|n| n.ends_with(".tmp"))
        .collect();
    assert!(
        leftovers.is_empty(),
        "temp files left behind: {leftovers:?}"
    );
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn name_escapes_are_rejected_on_disk_api() {
    let dir = temp_dir("escape");
    let store = FileStore::open(&dir, false).unwrap();
    for bad in ["../evil", "a/b", "..", "/etc/passwd"] {
        assert!(matches!(
            store.save(bad, &HierBitmap::new()),
            Err(hbs_store::StoreError::BadName(_))
        ));
    }
    // Nothing written outside the root.
    assert!(!Path::new("../evil.hbs").exists());
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn list_reports_only_store_files() {
    let dir = temp_dir("list");
    let store = FileStore::open(&dir, false).unwrap();
    store.save("one", &denseish_set()).unwrap();
    store.save("two", &denseish_set()).unwrap();
    fs::write(dir.join("random.txt"), b"ignore me").unwrap();
    let names: Vec<String> = store.list().unwrap().into_iter().map(|i| i.name).collect();
    assert_eq!(names, vec!["one".to_string(), "two".to_string()]);
    let _ = fs::remove_dir_all(&dir);
}
