//! Evidence 05 — filesystem persistence: contiguous writes, restart rescan,
//! tamper detection, path-escape rejection, and full chain decode from disk.

mod common;

use common::*;
use lz77b::core::decoder::{encode_next, ChainSession};
use lz77b::core::error::{Category, Code};
use lz77b::store::BlockStore;

#[test]
fn persists_a_chain_and_recovers_after_reopen() {
    let mut log = RunRecorder::start("05-persist-reopen");
    let dir = tempdir("persist");
    let input = fixture("sample1.bin");

    let store = BlockStore::open(&dir).unwrap();
    let mut session = ChainSession::new();
    let chunks: Vec<&[u8]> = input.chunks(512).collect();
    log.state("chunks", chunks.len());
    for (i, piece) in chunks.iter().enumerate() {
        let out = encode_next(&session, piece).unwrap();
        let rec = store.append_block("stream-A", &out.raw).unwrap();
        log.state(&format!("stored block {i} bytes"), rec.size);
        session.decode_raw(&out.raw).unwrap();
    }
    let on_disk = store.bytes_on_disk();
    log.state("bytes on disk", on_disk);
    drop(store);

    let reopened = BlockStore::open(&dir).unwrap();
    log.assert_eq_display(
        "rescanned block count",
        reopened.block_count("stream-A").unwrap(),
        chunks.len(),
        "rescan must rebuild the index from filenames",
    );
    log.assert_eq_display(
        "rescanned byte total",
        reopened.bytes_on_disk(),
        on_disk,
        "same bytes",
    );
    let decoded = reopened.decode_stream("stream-A").unwrap();
    if decoded != input {
        log.fail(
            "decode after reopen",
            "identical",
            "differ",
            "persistence roundtrip",
        );
    }
    log.assert_eq_display(
        "decoded bytes",
        decoded.len(),
        input.len(),
        "full sample recovered from disk",
    );
    log.finish(true);
}

#[test]
fn tampered_on_disk_block_is_rejected_on_decode() {
    let mut log = RunRecorder::start("05-tamper");
    let dir = tempdir("tamper");
    let store = BlockStore::open(&dir).unwrap();
    let mut session = ChainSession::new();
    let mut raws = Vec::new();
    for piece in [
        b"payload part one one one".as_slice(),
        b"part two two two".as_slice(),
    ] {
        let out = encode_next(&session, piece).unwrap();
        store.append_block("t", &out.raw).unwrap();
        session.decode_raw(&out.raw).unwrap();
        raws.push(out.raw);
    }

    // Corrupt a payload byte on disk directly.
    let p1 = dir.join("t").join("block-00000001.lzb");
    let mut bytes = std::fs::read(&p1).unwrap();
    let flip = bytes.len() - 2;
    bytes[flip] ^= 0x55;
    std::fs::write(&p1, bytes).unwrap();

    let err = BlockStore::open(&dir)
        .unwrap()
        .decode_stream("t")
        .unwrap_err();
    log.state("tamper code", err.code_name());
    assert_eq!(err.category(), Category::Input);
    // A payload flip hits CRC first.
    assert_eq!(err.code, Code::CrcMismatch);
    log.finish(true);
}

#[test]
fn path_traversal_ids_and_ordering_are_rejected() {
    let mut log = RunRecorder::start("05-store-contracts");
    let dir = tempdir("contracts");
    let store = BlockStore::open(&dir).unwrap();

    // A syntactically valid block — only the stream id is hostile. Append must
    // reject traversal/separator ids before touching the filesystem.
    let block = {
        let session = ChainSession::new();
        encode_next(&session, b"safe block").unwrap().raw
    };
    for bad in ["../etc/passwd", "a/b", ".."] {
        let err = store.append_block(bad, &block).unwrap_err();
        log.state(&format!("id {bad:?} code"), err.code_name());
        assert_eq!(err.code, Code::BadString);
        assert_eq!(err.category(), Category::Input);
    }
    // Further charset exclusions checked at the validator itself.
    for bad in ["", "\\share", "a b", "a$b", &"z".repeat(65)] {
        let err = lz77b::store::validate_stream_id(bad).unwrap_err();
        assert_eq!(err.code, Code::BadString);
        assert_eq!(err.category(), Category::Input);
    }

    // Out-of-order block append (starting at index 1) is a state conflict.
    let mut s = ChainSession::new();
    let _b0 = encode_next(&s, b"zero block zero block").unwrap();
    s.decode_raw(&_b0.raw).unwrap();
    let b1 = encode_next(&s, b"one block one block").unwrap();
    let err = store.append_block("ok", &b1.raw).unwrap_err();
    assert_eq!(err.code, Code::IndexGap);
    assert_eq!(err.category(), Category::State);

    // Missing stream/block reads are NotFound (state).
    let err = store.read_block("ghost", 0).unwrap_err();
    assert_eq!(err.code, Code::NotFound);
    log.state("missing-stream code", err.code_name());

    log.finish(true);
}

#[cfg(unix)]
#[test]
fn symlink_store_entries_are_refused() {
    use std::os::unix::fs::symlink;
    let mut log = RunRecorder::start("05-symlinks");
    let dir = tempdir("symlink");
    let store = BlockStore::open(&dir).unwrap();

    // A symlinked stream directory: rescan must not index it, append refuses.
    let outside = tempdir("symlink-target");
    let link = dir.join("evil");
    symlink(&outside, &link).unwrap();
    assert_eq!(store.block_count("evil").unwrap(), 0);
    let block = {
        let session = ChainSession::new();
        encode_next(&session, b"x").unwrap().raw
    };
    let err = store.append_block("evil", &block).unwrap_err();
    log.state("symlinked dir append category", err.category().to_string());
    assert!(
        std::fs::read_dir(&outside).unwrap().next().is_none(),
        "append must not write outside the store through a symlink"
    );

    // A symlinked block file inside an otherwise real stream is not indexed.
    std::fs::create_dir_all(dir.join("real")).unwrap();
    let bait = outside.join("bait.lzb");
    std::fs::write(&bait, b"external").unwrap();
    symlink(&bait, dir.join("real").join("block-00000000.lzb")).unwrap();
    let reopened = BlockStore::open(&dir).unwrap();
    assert_eq!(
        reopened.block_count("real").unwrap(),
        0,
        "symlink block not indexed"
    );
    assert_eq!(reopened.bytes_on_disk(), 0, "symlink bytes not counted");
    log.note(
        "symlinked stream dirs and block files are invisible to the index and writes fail closed",
    );
    log.finish(true);
}
