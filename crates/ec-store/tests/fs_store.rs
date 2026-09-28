//! Filesystem adapter tests: real temp directory, atomic commit order,
//! missing-file vs bad-content distinction, manifest tampering on disk.

use ec_core::error::EcError;
use ec_core::reed_solomon::encode;
use ec_core::{digest_shard, CodecConfig};
use ec_format::Manifest;
use ec_store::{
    manifest_path, shard_path, FileSystemStore, ObjectStore, ShardRead,
};

fn temp_dir(tag: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "ec-fs-test-{}-{}-{}",
        tag,
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn make_object(store: &dyn ObjectStore, id: &str) -> (CodecConfig, Vec<u8>, Vec<Vec<u8>>) {
    let cfg = CodecConfig::new(3, 2).unwrap();
    let data = b"filesystem-adapter-golden-input".to_vec();
    let enc = encode(&cfg, &data);
    let digests: Vec<(u16, Vec<u8>)> = enc
        .shards
        .iter()
        .enumerate()
        .map(|(i, b)| (i as u16, digest_shard(i as u16, b)))
        .collect();
    let manifest = Manifest::create(id, &cfg, enc.shard_len, data.len() as u64, digests).unwrap();
    store.put_object(&manifest, &enc.shards).unwrap();
    (cfg, data, enc.shards)
}

#[test]
fn writes_layout_and_reads_it_back() {
    let root = temp_dir("layout");
    let store = FileSystemStore::open(&root).unwrap();
    let (_, _, shards) = make_object(&store, "obj-abc");

    // Exact on-disk layout.
    assert!(manifest_path(&root, "obj-abc").exists());
    for i in 0..5u16 {
        let p = shard_path(&root, "obj-abc", i);
        assert!(p.exists(), "{p:?} missing");
        assert_eq!(std::fs::read(&p).unwrap(), shards[i as usize]);
    }
    // get_manifest verifies digest on read.
    let m = store.get_manifest("obj-abc").unwrap();
    assert_eq!(m.object_id, "obj-abc");
}

#[test]
fn missing_shard_file_is_classified_missing_not_error() {
    let root = temp_dir("missing");
    let store = FileSystemStore::open(&root).unwrap();
    make_object(&store, "obj-missing");

    // Present.
    assert!(matches!(
        store.read_shard("obj-missing", 0).unwrap(),
        ShardRead::Present(_)
    ));
    std::fs::remove_file(shard_path(&root, "obj-missing", 2)).unwrap();
    // Absent file: ShardRead::Missing, NOT an Err.
    assert!(matches!(
        store.read_shard("obj-missing", 2).unwrap(),
        ShardRead::Missing
    ));
    // Unknown object's manifest is a Store NOT_FOUND error instead.
    assert!(matches!(
        store.get_manifest("does-not-exist").unwrap_err(),
        EcError::Store(s) if s.contains("NOT_FOUND")
    ));
}

#[test]
fn tampered_manifest_on_disk_is_rejected() {
    // Two layers, two categories:
    //  - arithmetically consistent but unauthenticated length -> digest mismatch;
    //  - a lone inconsistent field -> structural rejection.
    let root = temp_dir("tamper");
    let store = FileSystemStore::open(&root).unwrap();
    make_object(&store, "obj-tamper"); // 31 bytes, shard_len 11, pad 2, cap 33
    let mp = manifest_path(&root, "obj-tamper");

    // Layer 1: consistent tamper (original_len 30 + pad_len 3 = cap 33)
    // passes arithmetic but the sealed manifest digest no longer matches.
    let mut v: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&mp).unwrap()).unwrap();
    v["original_len"] = serde_json::json!(30);
    v["pad_len"] = serde_json::json!(3);
    std::fs::write(&mp, serde_json::to_string_pretty(&v).unwrap()).unwrap();
    assert_eq!(
        store.get_manifest("obj-tamper").unwrap_err(),
        EcError::ManifestDigestMismatch
    );

    // Layer 2: restore and tamper one field only -> structural rejection.
    make_object(&store, "obj-tamper2");
    let mp2 = manifest_path(&root, "obj-tamper2");
    let mut v2: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&mp2).unwrap()).unwrap();
    v2["original_len"] = serde_json::json!(1);
    std::fs::write(&mp2, serde_json::to_string_pretty(&v2).unwrap()).unwrap();
    assert!(matches!(
        store.get_manifest("obj-tamper2").unwrap_err(),
        EcError::ManifestFieldMissing(_) | EcError::ManifestDigestMismatch
    ));
}

#[test]
fn write_shard_repairs_missing_file_and_rejects_bad_index() {
    let root = temp_dir("repair");
    let store = FileSystemStore::open(&root).unwrap();
    let (_, _, shards) = make_object(&store, "obj-repair");
    std::fs::remove_file(shard_path(&root, "obj-repair", 4)).unwrap();
    assert!(matches!(
        store.read_shard("obj-repair", 4).unwrap(),
        ShardRead::Missing
    ));
    store.write_shard("obj-repair", 4, &shards[4]).unwrap();
    match store.read_shard("obj-repair", 4).unwrap() {
        ShardRead::Present(b) => assert_eq!(b, shards[4]),
        other => panic!("expected present, got {other:?}"),
    }
    let err = store.write_shard("obj-repair", 99, &shards[0]).unwrap_err();
    assert!(matches!(err, EcError::InvalidShardIndex { .. }));
}

#[test]
fn double_put_is_rejected_and_unsafe_ids_are_blocked() {
    let root = temp_dir("double");
    let store = FileSystemStore::open(&root).unwrap();
    make_object(&store, "obj-once");

    // A second put with the same id is an error (not a silent overwrite).
    let cfg = CodecConfig::new(3, 2).unwrap();
    let data = b"different bytes here!!".to_vec();
    let enc = encode(&cfg, &data);
    let digests: Vec<(u16, Vec<u8>)> = enc
        .shards
        .iter()
        .enumerate()
        .map(|(i, b)| (i as u16, digest_shard(i as u16, b)))
        .collect();
    let m2 = Manifest::create("obj-once", &cfg, enc.shard_len, data.len() as u64, digests).unwrap();
    assert!(matches!(
        store.put_object(&m2, &enc.shards).unwrap_err(),
        EcError::Store(s) if s.contains("ALREADY_EXISTS")
    ));
    // First object's bytes must be untouched.
    let m = store.get_manifest("obj-once").unwrap();
    assert_eq!(m.original_len as usize, b"filesystem-adapter-golden-input".len());

    for bad in ["../escape", "a/b", ".hidden", ""] {
        let cfg1 = CodecConfig::new(1, 1).unwrap();
        let enc1 = encode(&cfg1, b"x");
        let digests1 = vec![
            (0u16, digest_shard(0, &enc1.shards[0])),
            (1u16, digest_shard(1, &enc1.shards[1])),
        ];
        let mb = Manifest::create(bad, &cfg1, enc1.shard_len, 1, digests1).unwrap();
        assert!(matches!(
            store.put_object(&mb, &enc1.shards).unwrap_err(),
            EcError::Store(_)
        ));
    }
}
