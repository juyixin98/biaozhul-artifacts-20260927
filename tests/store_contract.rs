//! Evidence suite #2: filesystem persistence adapter contract.
//!
//! Covers the stateful cases the stateless kernel cannot: missing predecessor,
//! broken chain (frame file deleted behind the manifest), stale optimistic pin,
//! cross-reload durability, and decoding a stored dependent chain against the
//! independent reference.

mod common;

use std::fs;

use common::*;
use lz77_blocks::error::ErrorCategory;
use lz77_blocks::store::BlockStore;
use serde_json::{json, Value};

fn temp_store(tag: &str) -> tempfile_lite::TempDir {
    tempfile_lite::TempDir::new(tag)
}

mod tempfile_lite {
    //! Minimal temp-dir helper (no extra dev-dependency): creates a unique dir
    //! under target/test-tmp and removes it on drop unless LZ77_KEEP_TMP is set.
    use std::fs;
    use std::path::PathBuf;

    pub struct TempDir {
        path: PathBuf,
    }

    impl TempDir {
        pub fn new(tag: &str) -> Self {
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("target/test-tmp/stores")
                .join(format!("{tag}-{}-{nanos}", std::process::id()));
            fs::create_dir_all(&path).unwrap();
            Self { path }
        }

        pub fn path(&self) -> &std::path::Path {
            &self.path
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            if std::env::var_os("LZ77_KEEP_TMP").is_none() {
                let _ = fs::remove_dir_all(&self.path);
            }
        }
    }
}

#[test]
fn independent_root_then_dependent_chain_persists_and_reloads() {
    let mut log = TestLog::new("store_contract");
    let mut failures: Vec<String> = Vec::new();
    let dir = temp_store("chain");
    let store = BlockStore::open(dir.path()).unwrap();

    let chunks: Vec<&[u8]> = vec![
        b"the quick brown fox. ",
        b"the quick brown fox jumps! ",
        b"the quick brown fox jumps over and over.",
    ];

    let root = store.put_independent(chunks[0]).unwrap();
    let c1 = store.put_dependent(chunks[1], Some(&root.id)).unwrap();
    let c2 = store.put_dependent(chunks[2], Some(&c1.id)).unwrap();

    // read_block returns exactly the block, read_chain the concatenation.
    let mut want_full = Vec::new();
    for (meta, chunk) in [(&root, chunks[0]), (&c1, chunks[1]), (&c2, chunks[2])] {
        let got = store.read_block(&meta.id).unwrap();
        want_full.extend_from_slice(chunk);
        if got != chunk {
            failures.push(format!("read_block {}", meta.id));
        }
        log.pass(
            &format!("read_block_{}", meta.sequence),
            "per-block decompression returns that block's bytes only",
            json!({"id": meta.id, "mode": meta.mode, "data_len": meta.data_len,
                   "payload_len": meta.payload_len, "roundtrip_equal": got == chunk}),
        );
    }
    let full = store.read_chain(None).unwrap();
    assert_eq!(full, want_full);

    // Reload: manifest + frame files are the durable state.
    drop(store);
    let reopened = BlockStore::open(dir.path()).unwrap();
    let full2 = reopened.read_chain(None).unwrap();
    if full2 == want_full {
        log.pass(
            "reload_durability",
            "after reopening the store the chain decodes identically",
            json!({"store_dir": dir.path().display().to_string(),
                   "files_on_disk": fs::read_dir(dir.path().join("blocks")).unwrap().count()}),
        );
    } else {
        failures.push("reload_durability".into());
        log.fail("reload_durability", "post-reload bytes differ", json!({}));
    }

    // Independent reference agrees with the stored frames, streamed in order.
    if oracle_available() {
        let mut combined: Vec<u8> = Vec::new();
        for id in [&root.id, &c1.id, &c2.id] {
            let frame = reopened.get_frame(id).unwrap();
            let (rep, out) = oracle_roundtrip(&frame, &combined);
            if !rep.valid() {
                failures.push(format!("oracle_{id}"));
                log.fail(
                    &format!("oracle_{id}"),
                    "independent reference rejected stored frame",
                    json!({"report": rep.json}),
                );
                break;
            }
            combined.extend_from_slice(&out.unwrap());
        }
        if combined == want_full {
            log.pass(
                "oracle_stored_chain",
                "Python reference streams all three stored frames to identical bytes",
                json!({}),
            );
        } else {
            failures.push("oracle_stored_chain".into());
        }
    }

    let path = log.finish();
    assert!(failures.is_empty(), "{failures:?}; log {path}");
}

#[test]
fn dependent_without_predecessor_is_state_conflict() {
    let mut log = TestLog::new("store_contract");
    let dir = temp_store("empty");
    let store = BlockStore::open(dir.path()).unwrap();

    let err = store.put_dependent(b"cannot append", None).unwrap_err();
    let ok = err.category == ErrorCategory::StateConflict && err.detail.contains("no root block");
    log.pass_or_record(
        ok,
        "dependent_on_empty_store",
        "append to empty store must be state_conflict (missing predecessor)",
        json!({"category": err.category.as_ref(), "detail": err.detail}),
    );
    println!("{}", log.finish());
    assert!(ok);
}

#[test]
fn stale_prev_id_pin_is_state_conflict() {
    let mut log = TestLog::new("store_contract");
    let dir = temp_store("pin");
    let store = BlockStore::open(dir.path()).unwrap();

    let root = store.put_independent(b"root v1").unwrap();
    let child = store.put_dependent(b"child", Some(&root.id)).unwrap();
    // A late writer still pins the root while the tip has advanced.
    let err = store
        .put_dependent(b"late append", Some(&root.id))
        .unwrap_err();
    let ok = err.category == ErrorCategory::StateConflict && err.detail.contains("tip advanced");
    log.pass_or_record(
        ok,
        "stale_pin",
        "optimistic pin against a non-tip predecessor is a state conflict",
        json!({"pinned": root.id, "tip": child.id,
               "category": err.category.as_ref(), "detail": err.detail}),
    );
    println!("{}", log.finish());
    assert!(ok);
}

#[test]
fn unknown_block_is_not_found_and_broken_chain_is_state_conflict() {
    let mut log = TestLog::new("store_contract");
    let mut failures: Vec<String> = Vec::new();
    let dir = temp_store("broken");
    let store = BlockStore::open(dir.path()).unwrap();
    let root = store.put_independent(b"root").unwrap();
    let child = store.put_dependent(b"child", Some(&root.id)).unwrap();

    let nf = store.read_block("blk-99999999").unwrap_err();
    if nf.category == ErrorCategory::NotFound {
        log.pass(
            "unknown_id",
            "unknown block id is not_found",
            json!({"category": nf.category.as_ref()}),
        );
    } else {
        failures.push("unknown_id".to_string());
        log.fail(
            "unknown_id",
            "wrong category",
            json!({"error": nf.to_string()}),
        );
    }

    // Delete the ROOT frame file behind the store's back: chain resolution must
    // classify this as a state conflict, not an input/compute error.
    let root_file = dir.path().join("blocks").join(format!("{}.lz71", root.id));
    fs::remove_file(&root_file).unwrap();
    let broken = store.read_block(&child.id).unwrap_err();
    if broken.category == ErrorCategory::StateConflict && broken.detail.contains("chain is broken")
    {
        log.pass(
            "missing_root_frame",
            "dependent block whose predecessor frame vanished is state_conflict",
            json!({"deleted": root_file.display().to_string(),
                   "queried": child.id,
                   "detail": broken.detail}),
        );
    } else {
        failures.push("missing_root_frame".to_string());
        log.fail(
            "missing_root_frame",
            "broken chain classified incorrectly",
            json!({"error": broken.to_string()}),
        );
    }

    let path = log.finish();
    assert!(failures.is_empty(), "{failures:?}; log {path}");
}

#[test]
fn manifest_never_points_at_unwritten_frame() {
    // The commit order (write+rename frame, then rewrite manifest) is an ordering
    // invariant. We assert the observable consequence: after every successful put,
    // both the manifest entry and a fully readable frame exist; no tmp leftovers.
    let mut log = TestLog::new("store_contract");
    let dir = temp_store("atomic");
    let store = BlockStore::open(dir.path()).unwrap();

    store.put_independent(b"a").unwrap();
    store.put_dependent(b"b", None).unwrap();
    let blocks = dir.path().join("blocks");
    let leftovers: Vec<String> = fs::read_dir(&blocks)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
        .filter(|n| n.ends_with(".tmp"))
        .collect();
    let manifest: Value =
        serde_json::from_slice(&fs::read(dir.path().join("manifest.json")).unwrap()).unwrap();
    let entry_count = manifest["blocks"].as_object().unwrap().len();
    let frame_count = fs::read_dir(&blocks).unwrap().count();

    let ok = leftovers.is_empty() && entry_count == 2 && frame_count == 2;
    log.pass_or_record(
        ok,
        "atomic_commit",
        "2 manifest entries <-> 2 frame files, zero .tmp leftovers",
        json!({"entries": entry_count, "frames": frame_count, "leftovers": leftovers}),
    );
    println!("{}", log.finish());
    assert!(ok);
}
