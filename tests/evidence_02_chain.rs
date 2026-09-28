//! Evidence 02 — chain state conflicts: missing predecessor, wrong digest,
//! index gaps, CRC and length corruption. Each case asserts the *specific*
//! failure code and its four-way category, and both the core and (where
//! applicable) the independent reference must classify it.

mod common;

use common::*;
use lz77b::core::constants::HEADER_LEN;
use lz77b::core::decoder::ChainSession;
use lz77b::core::error::{Category, Code};
use lz77b::core::format::{BlockHeader, FrameType};
use lz77b::reference;

struct TwoBlocks {
    b0: Vec<u8>,
    b1: Vec<u8>,
    dict_after_0: Vec<u8>,
}

fn make_two_block_chain() -> TwoBlocks {
    let plain0 = b"state conflict fixture phrase phrase phrase".repeat(3);
    let b0 = encode_indep(&plain0);
    let mut s = ChainSession::new();
    let got0 = s.decode_raw(&b0).unwrap();
    assert_eq!(got0, plain0);
    let dict_after_0 = s.dict().to_vec();

    let out1 = lz77b::core::encoder::encode_block(
        FrameType::Dependent,
        1,
        &dict_after_0,
        b"phrase again, phrase again",
    )
    .unwrap();
    // sanity: block1 decodes in the matching session
    s.decode_raw(&out1.raw).unwrap();
    TwoBlocks {
        b0,
        b1: out1.raw,
        dict_after_0,
    }
}

#[test]
fn missing_predecessor_first_block_dependent_is_state_conflict() {
    let mut log = RunRecorder::start("02-missing-predecessor");
    let chain = make_two_block_chain();

    // Fresh session receives index 1: no predecessor exists at all.
    let mut fresh = ChainSession::new();
    let err = fresh.decode_raw(&chain.b1).unwrap_err();
    log.state("observed code", err.code_name());
    log.state("observed category", err.category().to_string());
    log.state("detail", &err.detail);
    assert_eq!(err.code, Code::IndexGap);
    assert_eq!(err.category(), Category::State);
    log.note("index continuity fails before digest comparison: predecessor is absent.");
    log.finish(true);
}

#[test]
fn wrong_predecessor_digest_is_distinct_state_conflict() {
    let mut log = RunRecorder::start("02-wrong-digest");
    let chain = make_two_block_chain();

    // Session parked exactly after block 0 but with different dictionary.
    let foreign_plain = b"a completely unrelated predecessor body!!";
    let foreign_b0 = encode_indep(foreign_plain);
    let mut parked = ChainSession::new();
    parked.decode_raw(&foreign_b0).unwrap();

    // Flip a digest header byte while leaving payload (and its CRC) intact.
    let mut tampered = chain.b1.clone();
    tampered[10] ^= 0x01;
    let err = parked.decode_raw(&tampered).unwrap_err();
    log.state("observed code", err.code_name());
    assert_eq!(err.code, Code::DigestMismatch);
    assert_eq!(err.category(), Category::State);
    log.note("digest mismatch is distinguishable from index gap and from CRC failure.");

    // Same tampered block against the correct session: still a digest mismatch.
    let mut correct = ChainSession::new();
    correct.decode_raw(&chain.b0).unwrap();
    let err = correct.decode_raw(&tampered).unwrap_err();
    assert_eq!(err.code, Code::DigestMismatch);
    assert_eq!(err.category(), Category::State);
    log.finish(true);
}

#[test]
fn index_gap_and_duplicate_are_state_conflicts() {
    let mut log = RunRecorder::start("02-index-gap");
    let chain = make_two_block_chain();

    let mut s = ChainSession::new();
    s.decode_raw(&chain.b0).unwrap();
    s.decode_raw(&chain.b1).unwrap();
    // Replay b0: expects index 2 but gets 0.
    let err = s.decode_raw(&chain.b0).unwrap_err();
    log.state("replay-b0 code", err.code_name());
    assert_eq!(err.code, Code::IndexGap);
    assert_eq!(err.category(), Category::State);

    // Skip b0: a session at 0 receiving b1 already covered above; here also
    // check jumping ahead by 2 using a forged index.
    let mut forged = chain.b1.clone();
    forged[6..10].copy_from_slice(&5u32.to_be_bytes());
    let mut s2 = ChainSession::new();
    s2.decode_raw(&chain.b0).unwrap();
    let err = s2.decode_raw(&forged).unwrap_err();
    // CRC only covers payload, so the forged index reaches the IndexGap check.
    log.state("forged-index code", err.code_name());
    assert_eq!(err.code, Code::IndexGap);
    assert_eq!(err.category(), Category::State);
    log.finish(true);
}

#[test]
fn crc_and_length_corruption_are_input_errors() {
    let mut log = RunRecorder::start("02-corruption");
    let chain = make_two_block_chain();

    // Payload corruption => CRC mismatch (input category).
    let mut crc_bad = chain.b1.clone();
    crc_bad[HEADER_LEN] ^= 0x80;
    let mut s = ChainSession::new();
    s.decode_raw(&chain.b0).unwrap();
    let err = s.decode_raw(&crc_bad).unwrap_err();
    log.state("crc-bad code", err.code_name());
    assert_eq!(err.code, Code::CrcMismatch);
    assert_eq!(err.category(), Category::Input);

    // Declared length larger than tokens produce => LengthMismatch (input).
    // Need to recompute CRC? No: declared length is header-only; payload stays
    // valid, so CRC passes and decoding reaches the final length comparison.
    let mut len_bad = chain.b1.clone();
    let h = BlockHeader::decode(&len_bad).unwrap();
    let inflated = h.decompressed_len + 3;
    len_bad[22..30].copy_from_slice(&inflated.to_be_bytes());
    let mut s2 = ChainSession::new();
    s2.decode_raw(&chain.b0).unwrap();
    let err = s2.decode_raw(&len_bad).unwrap_err();
    log.state("length-bad code", err.code_name());
    // Overrun is actually detected *during* append (declared bound exceeded),
    // which is LengthMismatch too.
    assert_eq!(err.code, Code::LengthMismatch);
    assert_eq!(err.category(), Category::Input);

    // Magic corruption => BadMagic.
    let mut magic_bad = chain.b0.clone();
    magic_bad[0] = b'X';
    let mut s3 = ChainSession::new();
    let err = s3.decode_raw(&magic_bad).unwrap_err();
    assert_eq!(err.code, Code::BadMagic);
    assert_eq!(err.category(), Category::Input);
    log.state("magic-bad code", err.code_name());
    log.finish(true);
}

#[test]
fn reference_decompressor_rejects_dependent_with_short_dict_and_shows_garbage() {
    let mut log = RunRecorder::start("02-reference-state");
    let chain = make_two_block_chain();

    // A *shorter* dictionary than the block's matches require: the reference
    // must reject this structurally (distance reaches before history).
    let short_dict: Vec<u8> = b"ab".to_vec();
    let err =
        reference::decompress_raw(&chain.b1, &short_dict).expect_err("must reject short dict");
    log.state("short-dict kind", format!("{:?}", err.kind));
    log.state("short-dict reason", &err.reason);
    assert_eq!(err.kind, reference::RefErrorKind::Input);

    // A same-length-but-wrong dictionary is structurally decodable for a
    // stateless parser — it yields garbage rather than an error. That is
    // exactly why the wire format binds a *digest*: only the chain session
    // (which recomputes it) can detect this case. Demonstrate both facts.
    let wrong_dict: Vec<u8> = vec![b'z'; chain.dict_after_0.len()];
    let garbage =
        reference::decompress_raw(&chain.b1, &wrong_dict).expect("wrong-but-long dict decodes");
    let correct = reference::decompress_raw(&chain.b1, &chain.dict_after_0).unwrap();
    assert_ne!(
        garbage, correct,
        "wrong dictionary must not yield correct bytes"
    );
    log.note("wrong same-length dict decodes to garbage — the digest binding (state error in the session) is the defense, not distance checks.");

    // And the session indeed rejects that block when bound to a different digest.
    let foreign_b0 = encode_indep(b"a completely unrelated predecessor body!!");
    let mut parked = ChainSession::new();
    parked.decode_raw(&foreign_b0).unwrap();
    let err = parked.decode_raw(&chain.b1).unwrap_err();
    assert_eq!(err.code, Code::DigestMismatch);
    assert_eq!(err.category(), Category::State);

    let mut s = ChainSession::new();
    s.decode_raw(&chain.b0).unwrap();
    let core = s.decode_raw(&chain.b1).unwrap();
    assert_eq!(core, correct);
    log.assert_eq_display(
        "reference agrees on bytes with correct dict",
        correct.len(),
        core.len(),
        "correct predecessor dictionary binds block 1",
    );
    log.finish(true);
}
