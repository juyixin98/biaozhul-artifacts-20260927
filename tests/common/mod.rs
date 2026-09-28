//! Shared helpers for evidence integration tests.
#![allow(dead_code)]

use lz77b::core::decoder::ChainSession;
use lz77b::core::encoder::encode_block;
use lz77b::core::format::FrameType;

#[path = "recorder.rs"]
pub mod recorder;

pub use recorder::RunRecorder;

/// Repo-root path (tests run with CARGO_MANIFEST_DIR set).
pub fn manifest_dir() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

pub fn fixture(name: &str) -> Vec<u8> {
    std::fs::read(manifest_dir().join("fixtures").join(name))
        .unwrap_or_else(|e| panic!("missing fixture {name}: {e}"))
}

pub fn fixture_block(n: u32) -> Vec<u8> {
    std::fs::read(
        manifest_dir()
            .join("fixtures")
            .join("blocks")
            .join(format!("block-{n:08}.lzb")),
    )
    .unwrap_or_else(|e| panic!("missing fixture block {n}: {e}"))
}

/// Encode an input as a chain at `chunk_size`, returning raw blocks and the
/// final dictionary bytes.
pub fn encode_at(input: &[u8], chunk_size: usize) -> (Vec<Vec<u8>>, Vec<u8>) {
    let mut session = ChainSession::new();
    let mut raws = Vec::new();
    for piece in input.chunks(chunk_size) {
        let out = lz77b::core::decoder::encode_next(&session, piece).unwrap();
        session.decode_raw(&out.raw).unwrap();
        raws.push(out.raw);
    }
    let dict = session.dict().to_vec();
    (raws, dict)
}

/// Independent encode helper (test inputs are always <= MAX_OUTPUT).
pub fn encode_indep(input: &[u8]) -> Vec<u8> {
    encode_block(FrameType::Independent, 0, &[], input)
        .unwrap()
        .raw
}

/// Unique temp dir per call.
pub fn tempdir(tag: &str) -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!(
        "lz77b-it-{}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        tag
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}
