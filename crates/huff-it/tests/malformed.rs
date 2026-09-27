//! Deliberately malformed containers: every case asserts the exact failure
//! category in both decoders, not merely "some error happened".

use huff_core::container::{decode_container, parse_container};
use huff_core::error::HuffError;
use huff_core::report;
use huff_it::indie;

/// Errors that can be triggered by mutating a block payload (frame and
/// directory CRCs are recomputed by [`rebuild_single`]).
use huff_core::container::HEADER_LEN as H;

const FRAME_OH: usize = 8;
const DIR_ENT: usize = 16;
const FOOTER: usize = 16;
const PAYLOAD_HEAD: usize = 12;
const LEN_OFF: usize = PAYLOAD_HEAD; // 256 length bytes start here
const BODY_OFF: usize = PAYLOAD_HEAD + 256;

fn crc(data: &[u8]) -> u32 {
    // Independent table-less CRC for test mutation.
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0xEDB8_8320u32 & 0u32.wrapping_sub(crc & 1));
        }
    }
    crc ^ 0xFFFF_FFFF
}

/// Build a single-block container around a mutated payload; requires the
/// declared original length to rebuild the directory entry.
fn rebuild_single(payload: &[u8], orig_len: u32, block_size: u32) -> Vec<u8> {
    let frame_len = (FRAME_OH + payload.len()) as u32;
    let dir_offset = H as u32 + frame_len;

    let mut header = Vec::new();
    header.extend_from_slice(b"HUFF");
    header.push(1);
    header.push(0);
    header.extend_from_slice(&block_size.to_le_bytes());
    header.extend_from_slice(&orig_len.to_le_bytes());
    header.extend_from_slice(&1u32.to_le_bytes());
    header.extend_from_slice(&dir_offset.to_le_bytes());
    header.extend_from_slice(&(DIR_ENT as u32).to_le_bytes());
    header.extend_from_slice(&0u16.to_le_bytes());
    let hcrc = crc(&header);
    header.extend_from_slice(&hcrc.to_le_bytes());

    let mut frame = Vec::new();
    frame.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    frame.extend_from_slice(&crc(payload).to_le_bytes());
    frame.extend_from_slice(payload);

    let mut dir = Vec::new();
    dir.extend_from_slice(&orig_len.to_le_bytes());
    dir.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    dir.extend_from_slice(&(H as u32).to_le_bytes());
    // Original CRC zero: malformed cases must fail before the CRC check.
    dir.extend_from_slice(&0u32.to_le_bytes());
    let dcrc = crc(&dir);

    let mut out = Vec::new();
    out.extend_from_slice(&header);
    out.extend_from_slice(&frame);
    out.extend_from_slice(&dir);
    out.extend_from_slice(&dcrc.to_le_bytes());
    out.extend_from_slice(&dir_offset.to_le_bytes());
    out.extend_from_slice(&(DIR_ENT as u32).to_le_bytes());
    out.extend_from_slice(&1u32.to_le_bytes());
    assert_eq!(out.len(), dir_offset as usize + DIR_ENT + FOOTER);
    out
}

use huff_core::container::encode_container;

fn assert_both_fail(blob: &[u8], want: HuffError, tag: &str) {
    let p = match decode_container(blob) {
        Err(e) => e,
        Ok(_) => panic!("[{tag}] production decoder unexpectedly succeeded"),
    };
    assert_eq!(p, want, "[{tag}] production: got {p:?}, want {want:?}");
    let i = indie::decode(blob).unwrap_err();
    assert_eq!(i.code(), want.code(), "[{tag}] independent: got {i}, want {want:?}");
    let rep = report::validate(blob);
    assert!(!rep.ok, "[{tag}] report should fail");
}

fn mut_payload(data: &[u8]) -> (Vec<u8>, u32, u32) {
    let blob = encode_container(data, 4096).unwrap();
    let payload_len = u32::from_le_bytes(blob[H..H + 4].try_into().unwrap());
    let block_size = u32::from_le_bytes(blob[6..10].try_into().unwrap());
    let payload = blob[H + FRAME_OH..H + FRAME_OH + payload_len as usize].to_vec();
    (payload, data.len() as u32, block_size)
}

#[test]
fn oversubscribed_trees_are_rejected() {
    // Three length-1 codes: Kraft 3/2.
    let mut payload = vec![0u8; BODY_OFF];
    payload[0..4].copy_from_slice(&3u32.to_le_bytes());
    payload[4..8].copy_from_slice(&0u32.to_le_bytes());
    payload[8..10].copy_from_slice(&3u16.to_le_bytes());
    payload[LEN_OFF + 0] = 1;
    payload[LEN_OFF + 1] = 1;
    payload[LEN_OFF + 2] = 1;
    let blob = rebuild_single(&payload, 3, 4096);
    assert_both_fail(&blob, HuffError::TableKraftOverflow, "kraft-3x-len1");

    // 17 length-4 codes in a 16-code space.
    let (mut p, orig, bs) = mut_payload(b"abcdefghij");
    for b in p[LEN_OFF..LEN_OFF + 256].iter_mut() {
        *b = 0;
    }
    for s in 0..17u16 {
        p[LEN_OFF + s as usize] = 4;
    }
    p[0..4].copy_from_slice(&orig.to_le_bytes());
    p[4..8].copy_from_slice(&0u32.to_le_bytes());
    p[8..10].copy_from_slice(&17u16.to_le_bytes());
    p.truncate(BODY_OFF);
    let blob = rebuild_single(&p, orig, bs);
    assert_both_fail(&blob, HuffError::TableKraftOverflow, "kraft-17-len4");
}

#[test]
fn code_length_over_32_rejected() {
    let (mut p, orig, bs) = mut_payload(b"hi there!!");
    p[LEN_OFF] = 33;
    // Force a positive-length count change by keeping symbol_count as stored
    // (this symbol already had a positive length for typical text).
    let blob = rebuild_single(&p, orig, bs);
    assert_both_fail(&blob, HuffError::CodeLenTooLong, "len-33");
}

#[test]
fn truncated_codeword_mid_stream() {
    let data = b"the independent decoder must detect a half-finished codeword!!";
    let (mut p, orig, bs) = mut_payload(data);
    let bits = u32::from_le_bytes(p[4..8].try_into().unwrap());
    p[4..8].copy_from_slice(&(bits - 2).to_le_bytes());
    let blob = rebuild_single(&p, orig, bs);
    assert_both_fail(&blob, HuffError::TruncatedCodeword, "truncated-codeword");
}

#[test]
fn bad_padding_bits_are_rejected() {
    // Pick an input whose total bit count is not a multiple of 8.
    let mut chosen: Option<Vec<u8>> = None;
    for pad in 0..40u8 {
        let data: Vec<u8> = b"padding probe".iter().chain(std::iter::once(&pad)).copied().collect();
        let (p, _, _) = mut_payload(&data);
        let bits = u32::from_le_bytes(p[4..8].try_into().unwrap());
        if bits % 8 != 0 {
            chosen = Some(data);
            break;
        }
    }
    let data = chosen.expect("find a non-byte-aligned input");
    let (mut p, orig, bs) = mut_payload(&data);
    let bits = u32::from_le_bytes(p[4..8].try_into().unwrap());
    let idx = BODY_OFF + (bits >> 3) as usize;
    let shift = bits & 7;
    p[idx] |= 0x80 >> shift; // first padding bit -> 1
    let blob = rebuild_single(&p, orig, bs);
    assert_both_fail(&blob, HuffError::InvalidPadding, "bad-padding");
}

#[test]
fn inflated_length_cannot_cause_unbounded_output() {
    // Declare more output bytes than the bitstream encodes: decoder must stop
    // at the bit boundary (truncated), never emit beyond the stream.
    let (mut p, orig, bs) = mut_payload(b"bounded output only please!!");
    p[0..4].copy_from_slice(&(orig + 1000).to_le_bytes());
    let blob = rebuild_single(&p, orig + 1000, bs);
    let prod = decode_container(&blob).unwrap_err();
    assert!(
        prod == HuffError::TruncatedCodeword || prod == HuffError::OutputLengthMismatch,
        "unbounded/length failure expected, got {prod:?}"
    );
    let ind = indie::decode(&blob).unwrap_err();
    assert!(
        ind.code() == "TRUNCATED_CODEWORD" || ind.code() == "OUTPUT_LENGTH_MISMATCH",
        "independent: {ind}"
    );
}

#[test]
fn bitstream_declared_past_body_rejected() {
    let (mut p, orig, bs) = mut_payload(b"bits past body probe");
    let bits = u32::from_le_bytes(p[4..8].try_into().unwrap());
    p[4..8].copy_from_slice(&(bits + 16).to_le_bytes());
    let blob = rebuild_single(&p, orig, bs);
    assert_both_fail(&blob, HuffError::BitstreamTruncated, "bits-past-body");
}

#[test]
fn one_symbol_with_positive_length_is_incomplete() {
    let mut p = vec![0u8; BODY_OFF];
    p[0..4].copy_from_slice(&5u32.to_le_bytes());
    p[4..8].copy_from_slice(&0u32.to_le_bytes());
    p[8..10].copy_from_slice(&1u16.to_le_bytes());
    p[10] = b'q';
    p[LEN_OFF + 7] = 1;
    let blob = rebuild_single(&p, 5, 4096);
    assert_both_fail(&blob, HuffError::TableIncomplete, "one-symbol-positive-len");
}

#[test]
fn symbol_count_mismatch_rejected() {
    let (mut p, orig, bs) = mut_payload(b"count mismatch probe value");
    let count = u16::from_le_bytes(p[8..10].try_into().unwrap());
    p[8..10].copy_from_slice(&(count + 1).to_le_bytes());
    let blob = rebuild_single(&p, orig, bs);
    assert_both_fail(&blob, HuffError::BadSymbolCount, "symbol-count+1");
}

#[test]
fn envelope_level_failures() {
    let blob = encode_container(b"envelope failure corpus", 4096).unwrap();

    // Bad magic (CRC also breaks; magic is checked first).
    let mut b = blob.clone();
    b[0] = b'X';
    assert_both_fail(&b, HuffError::BadMagic, "magic");

    // Unknown version — recompute the header CRC so the gate is the version.
    let mut b = blob.clone();
    b[4] = 42;
    let hc = crc(&b[..28]);
    b[28..32].copy_from_slice(&hc.to_le_bytes());
    assert_both_fail(&b, HuffError::UnknownVersion, "version-42");

    // Header tampering without CRC repair.
    let mut b = blob.clone();
    b[10] ^= 0xFF;
    assert_eq!(parse_container(&b).unwrap_err(), HuffError::HeaderCrcMismatch);

    // Directory tampering.
    let info = parse_container(&blob).unwrap();
    let mut b = blob.clone();
    b[info.dir_offset as usize] ^= 0x01;
    assert_both_fail(&b, HuffError::DirectoryCrcMismatch, "dir-crc");

    // Truncated files at several cut points.
    for &cut in &[0usize, 4, 31, H, blob.len() - 1] {
        let cut = cut.min(blob.len());
        let err = parse_container(&blob[..cut]).unwrap_err();
        assert!(
            matches!(
                err,
                HuffError::HeaderTruncated
                    | HuffError::DirectoryTruncated
                    | HuffError::BlockFrameTruncated
                    | HuffError::TrailingData
            ),
            "cut {cut}: unexpected {err:?}"
        );
    }

    // Trailing byte.
    let mut b = blob.clone();
    b.push(0);
    assert_both_fail(&b, HuffError::TrailingData, "trailing");
}

#[test]
fn block_payload_crc_tampering_is_isolated() {
    let blob = encode_container(b"crc tampering test payload", 4096).unwrap();
    let mut b = blob.clone();
    b[H + FRAME_OH + BODY_OFF] ^= 0xFF; // flip a body byte
    assert_eq!(parse_container(&b).unwrap_err(), HuffError::BlockCrcMismatch);
    assert_eq!(indie::decode(&b).unwrap_err().code(), "BLOCK_CRC_MISMATCH");
}

#[test]
fn directory_gap_is_not_contiguous() {
    // Two blocks; move the second frame offset in its directory entry.
    let data = vec![0x33u8; 4096 + 5];
    let mut blob = encode_container(&data, 4096).unwrap();
    let info = parse_container(&blob).unwrap();
    assert!(info.block_count >= 2);
    let e1 = info.dir_offset as usize + DIR_ENT;
    // frame_offset field sits at +8 inside the entry.
    blob[e1 + 8..e1 + 12].copy_from_slice(&(info.blocks[1].frame_offset + 1).to_le_bytes());
    // Directory CRC intentionally left stale: the CRC is the first gate.
    assert_eq!(
        parse_container(&blob).unwrap_err(),
        HuffError::DirectoryCrcMismatch,
        "stale directory CRC must be reported first"
    );
}

#[test]
fn total_length_field_must_match_blocks() {
    let blob = encode_container(b"total length accounting", 4096).unwrap();
    let mut b = blob;
    let info = parse_container(&b.clone().as_slice()).unwrap();
    let total = info.original_total;
    b[10..14].copy_from_slice(&(total + 1).to_le_bytes());
    let hc = crc(&b[..28]);
    b[28..32].copy_from_slice(&hc.to_le_bytes());
    assert_both_fail(&b, HuffError::TotalLengthMismatch, "total+1");
}
