//! 持久化层测试：原子往返、命名安全、损坏拒绝、JSON 夹具导入。

use std::collections::BTreeSet;

use rb_format::{codec, CodecError, RoaringSet};
use rb_persist::fixtures;
use rb_persist::Store;

fn temp_store() -> (Store, std::path::PathBuf) {
    let dir = std::env::temp_dir().join(format!(
        "rbpersist-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    (Store::open(&dir).unwrap(), dir)
}

#[test]
fn save_load_roundtrip_matches_contents() {
    let (store, _dir) = temp_store();
    let mut s = RoaringSet::new();
    for v in [0u32, 1, 4095, 4096, 65536, 70000, u32::MAX] {
        s.insert(v);
    }
    store.save("alpha", &s).unwrap();
    let loaded = store.load("alpha").unwrap();
    assert_eq!(loaded, s);
    assert!(store.exists("alpha"));
}

#[test]
fn list_only_lists_valid_rbs_sets() {
    let (store, dir) = temp_store();
    store.save("zeta", &RoaringSet::new().into_one(1)).unwrap();
    store.save("alpha", &RoaringSet::new().into_one(2)).unwrap();
    // 干扰文件：临时文件、其它后缀、非法 stem —— 都不能被列出
    std::fs::write(dir.join("stray.txt"), b"x").unwrap();
    std::fs::write(dir.join("zeta.rbs.tmp-1-2"), b"y").unwrap();
    std::fs::write(dir.join("bad name.rbs"), b"z").unwrap();

    let names = store.list().unwrap();
    assert_eq!(names, vec!["alpha".to_string(), "zeta".to_string()]);
}

#[test]
fn invalid_names_are_rejected_no_directory_escape() {
    let (store, _dir) = temp_store();
    for bad in [
        "",
        "a/b",
        "..",
        "../x",
        "a.b",
        "x:y",
        "a\\b",
        &"x".repeat(65),
    ] {
        assert!(matches!(
            store.save(bad, &RoaringSet::new()),
            Err(rb_persist::StoreError::InvalidName(_))
        ));
        assert!(matches!(
            store.load(bad),
            Err(rb_persist::StoreError::InvalidName(_))
        ));
    }
}

#[test]
fn not_found_distinct_from_corruption() {
    let (store, _dir) = temp_store();
    match store.load("ghost") {
        Err(rb_persist::StoreError::NotFound(n)) => assert_eq!(n, "ghost"),
        other => panic!("expected NotFound, got {other:?}"),
    }
}

#[test]
fn save_new_conflicts_on_existing() {
    let (store, _dir) = temp_store();
    store
        .save_new("once", &RoaringSet::new().into_one(1))
        .unwrap();
    assert!(matches!(
        store.save_new("once", &RoaringSet::new().into_one(2)),
        Err(rb_persist::StoreError::AlreadyExists(n)) if n == "once"
    ));
}

#[test]
fn corrupt_file_is_rejected_with_codec_category() {
    let (store, dir) = temp_store();
    let s = RoaringSet::from_values(0..5000u32);
    store.save("victim", &s).unwrap();
    let path = dir.join("victim.rbs");

    // 翻转体区一个字节 → body CRC 不匹配（而不是 panic 或 500）
    let mut bytes = std::fs::read(&path).unwrap();
    let n = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
    bytes[16 + n * 16 + 100] ^= 0x01;
    std::fs::write(&path, &bytes).unwrap();
    match store.load("victim") {
        Err(rb_persist::StoreError::Codec(CodecError::BodyChecksumMismatch { .. })) => {}
        other => panic!("expected BodyChecksumMismatch, got {other:?}"),
    }

    // 原子写保证：重新 save 后文件恢复可读
    store.save("victim", &s).unwrap();
    assert_eq!(store.load("victim").unwrap(), s);
}

#[test]
fn json_fixture_import_dedup_and_range_check() {
    let set = fixtures::parse_json(b"[5, 5, 1, 65536, 4294967295]").unwrap();
    let expected: BTreeSet<u32> = [1, 5, 65536, u32::MAX].into_iter().collect();
    assert_eq!(set.to_vec().into_iter().collect::<BTreeSet<_>>(), expected);

    match fixtures::parse_json(b"[1, -2]") {
        Err(fixtures::FixtureError::OutOfRange(-2)) => {}
        other => panic!("expected OutOfRange, got {other:?}"),
    }
    assert!(matches!(
        fixtures::parse_json(b"42"),
        Err(fixtures::FixtureError::NotAnArray)
    ));
    // 往返导出是排序紧凑 JSON
    assert_eq!(fixtures::to_json(&set), b"[1,5,65536,4294967295]");
}

#[test]
fn overwrite_is_atomic_and_consistent() {
    // 重复保存多个版本，任何时点加载都必须是完整的某一版（无半截文件）。
    let (store, _dir) = temp_store();
    for round in 0..20u32 {
        let s = RoaringSet::from_values((0..1000u32).map(|i| i.wrapping_add(round * 97)));
        store.save("versioned", &s).unwrap();
        let got = store.load("versioned").unwrap();
        assert_eq!(got, s, "round {round}");
    }
    // 临时文件不应残留
    let leftovers: Vec<_> = std::fs::read_dir(store.base())
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.contains(".tmp-"))
        .collect();
    assert!(
        leftovers.is_empty(),
        "temp files left behind: {leftovers:?}"
    );
}

// 小构造辅助（避免在每个测试里写循环）。
trait One {
    fn into_one(self, v: u32) -> Self;
}
impl One for RoaringSet {
    fn into_one(mut self, v: u32) -> Self {
        self.insert(v);
        self
    }
}

#[test]
fn codec_header_layout_is_documented_shape() {
    let s = RoaringSet::from_values(0..10u32);
    let bytes = codec::encode(&s);
    assert_eq!(&bytes[0..4], b"RBS1");
    assert_eq!(
        u32::from_le_bytes(bytes[4..8].try_into().unwrap()),
        0x0001_0000
    );
    assert_eq!(u32::from_le_bytes(bytes[8..12].try_into().unwrap()), 1);
    // 头 CRC 自洽
    let stored = u32::from_le_bytes(bytes[12..16].try_into().unwrap());
    assert_eq!(stored, rb_format::crc32c::checksum(&bytes[..12]));
}
