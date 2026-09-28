//! The `HCMP` container: fixed global header, block directory and packed
//! payload region.
//!
//! # Layout
//!
//! ```text
//! offset  size  field
//! 0       4     magic            = b"HCMP"
//! 4       1     version          = 1
//! 5       1     flags            = 0 (any set bit is rejected)
//! 6       2     reserved         = 0
//! 8       4     block_count      u32 BE
//! 12      4     directory_crc32  CRC-32 of the directory bytes
//! 16      ...   directory: block_count * 32-byte entries (see below)
//! ...     ...   payload region: blocks packed back-to-back, no gaps
//! ```
//!
//! Directory entry (32 bytes, all integers big-endian):
//!
//! ```text
//! block_id      u32   (0..4)
//! original_len  u64   (4..12)
//! payload_off   u64   (12..20, absolute file offset)
//! payload_len   u32   (20..24)
//! payload_crc32 u32   (24..28, CRC of the bytes at rest)
//! original_crc32 u32  (28..32, CRC of the decoded original bytes)
//! ```
//!
//! Integrity is enforced in layers:
//! 1. magic / version / flags are checked before anything is trusted;
//! 2. `directory_crc32` authenticates the directory;
//! 3. offsets must be strictly contiguous and non-overlapping, every payload
//!    must lie inside the file, and the final payload must end exactly at EOF
//!    (no trailing garbage);
//! 4. `payload_crc32` authenticates each payload *at rest*;
//! 5. after block decoding, the original length and CRC are checked.
//!
//! Block ids must be unique.

use crate::block::{decode_block, encode_block, EncodedBlock};
use crate::crc32::checksum;
use crate::error::{Error, ErrorKind, Result};

/// Container magic bytes.
pub const MAGIC: [u8; 4] = *b"HCMP";
/// Only this format version is accepted.
pub const VERSION: u8 = 1;
/// Global header size.
pub const GLOBAL_HEADER_LEN: usize = 16;
/// One fixed-size directory entry.
pub const DIR_ENTRY_LEN: usize = 32;
/// Hard safety cap on the number of blocks per container.
pub const MAX_BLOCKS: u32 = 1 << 20;

/// Metadata for one block.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockMeta {
    /// Block identifier within the container.
    pub block_id: u32,
    /// Original byte count.
    pub original_len: u64,
    /// CRC-32 of the original bytes.
    pub original_crc: u32,
    /// Payload byte count.
    pub payload_len: u32,
}

/// One decoded block: metadata plus reconstructed bytes.
#[derive(Debug, Clone)]
pub struct DecodedBlock {
    /// Metadata.
    pub meta: BlockMeta,
    /// Decoded original bytes.
    pub data: Vec<u8>,
}

#[derive(Debug, Clone, Copy)]
struct DirEntry {
    block_id: u32,
    original_len: u64,
    payload_off: u64,
    payload_len: u32,
    payload_crc32: u32,
    original_crc32: u32,
}

impl DirEntry {
    fn write(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.block_id.to_be_bytes());
        out.extend_from_slice(&self.original_len.to_be_bytes());
        out.extend_from_slice(&self.payload_off.to_be_bytes());
        out.extend_from_slice(&self.payload_len.to_be_bytes());
        out.extend_from_slice(&self.payload_crc32.to_be_bytes());
        out.extend_from_slice(&self.original_crc32.to_be_bytes());
    }

    fn read(buf: &[u8]) -> Self {
        debug_assert_eq!(buf.len(), DIR_ENTRY_LEN);
        DirEntry {
            block_id: u32::from_be_bytes(buf[0..4].try_into().unwrap()),
            original_len: u64::from_be_bytes(buf[4..12].try_into().unwrap()),
            payload_off: u64::from_be_bytes(buf[12..20].try_into().unwrap()),
            payload_len: u32::from_be_bytes(buf[20..24].try_into().unwrap()),
            payload_crc32: u32::from_be_bytes(buf[24..28].try_into().unwrap()),
            original_crc32: u32::from_be_bytes(buf[28..32].try_into().unwrap()),
        }
    }
}

/// Encode a container from independent input chunks (each chunk becomes one
/// independently coded block). Block ids are assigned `0..chunks.len()` in
/// order.
pub fn encode_container(chunks: &[Vec<u8>], max_block_len: u64) -> Result<Vec<u8>> {
    let block_count = chunks.len();
    if block_count as u64 > MAX_BLOCKS as u64 {
        return Err(Error::new(
            ErrorKind::TooManyBlocks,
            format!("{block_count} blocks exceeds cap {MAX_BLOCKS}"),
        ));
    }

    let encoded: Vec<EncodedBlock> = chunks
        .iter()
        .map(|c| encode_block(c, max_block_len))
        .collect::<Result<Vec<_>>>()?;

    let mut cursor = GLOBAL_HEADER_LEN as u64 + DIR_ENTRY_LEN as u64 * block_count as u64;
    let entries: Vec<DirEntry> = encoded
        .iter()
        .enumerate()
        .map(|(i, enc)| {
            let off = cursor;
            cursor += enc.payload.len() as u64;
            DirEntry {
                block_id: i as u32,
                original_len: enc.original_len,
                payload_off: off,
                payload_len: enc.payload.len() as u32,
                payload_crc32: enc.payload_crc,
                original_crc32: enc.original_crc,
            }
        })
        .collect();

    let mut directory = Vec::with_capacity(DIR_ENTRY_LEN * block_count);
    for e in &entries {
        e.write(&mut directory);
    }
    let directory_crc = checksum(&directory);

    let mut out = Vec::with_capacity(cursor as usize);
    out.extend_from_slice(&MAGIC);
    out.push(VERSION);
    out.push(0); // flags
    out.extend_from_slice(&[0, 0]); // reserved
    out.extend_from_slice(&(block_count as u32).to_be_bytes());
    out.extend_from_slice(&directory_crc.to_be_bytes());
    out.extend_from_slice(&directory);
    for enc in &encoded {
        out.extend_from_slice(&enc.payload);
    }
    debug_assert_eq!(out.len() as u64, cursor);
    Ok(out)
}

/// Container-level summary.
#[derive(Debug, Clone)]
pub struct ContainerInfo {
    /// Format version byte.
    pub version: u8,
    /// Block metadata in directory order.
    pub blocks: Vec<BlockMeta>,
}

/// Parse and verify container structure without decoding bodies.
pub fn inspect(bytes: &[u8]) -> Result<ContainerInfo> {
    if bytes.len() < GLOBAL_HEADER_LEN {
        return Err(Error::new(
            ErrorKind::TruncatedHeader,
            format!(
                "file is {} bytes, need at least {GLOBAL_HEADER_LEN}",
                bytes.len()
            ),
        ));
    }
    if bytes[0..4] != MAGIC {
        return Err(Error::new(
            ErrorKind::BadMagic,
            format!("magic is {:?}, expected {:?}", &bytes[0..4], MAGIC),
        ));
    }
    let version = bytes[4];
    if version != VERSION {
        return Err(Error::new(
            ErrorKind::UnknownVersion,
            format!(
                "container version {version} is not supported; this build speaks version {VERSION}"
            ),
        ));
    }
    let flags = bytes[5];
    if flags != 0 {
        return Err(Error::new(
            ErrorKind::BadFlags,
            format!("reserved header flags set: {flags:#04x}"),
        ));
    }
    if bytes[6..8] != [0, 0] {
        return Err(Error::new(
            ErrorKind::BadFlags,
            "reserved header bytes 6..8 are non-zero",
        ));
    }

    let block_count = u32::from_be_bytes(bytes[8..12].try_into().unwrap()) as usize;
    let directory_crc_stored = u32::from_be_bytes(bytes[12..16].try_into().unwrap());

    if block_count as u32 > MAX_BLOCKS {
        return Err(Error::new(
            ErrorKind::TooManyBlocks,
            format!("block_count {block_count} exceeds cap {MAX_BLOCKS}"),
        ));
    }

    let dir_len = DIR_ENTRY_LEN
        .checked_mul(block_count)
        .ok_or_else(|| Error::new(ErrorKind::TooManyBlocks, "directory size overflow"))?;
    let dir_end = GLOBAL_HEADER_LEN
        .checked_add(dir_len)
        .ok_or_else(|| Error::new(ErrorKind::TruncatedDirectory, "directory end overflow"))?;
    if dir_end > bytes.len() {
        return Err(Error::new(
            ErrorKind::TruncatedDirectory,
            format!(
                "directory needs {dir_len} bytes but file is only {}",
                bytes.len()
            ),
        ));
    }

    let directory = &bytes[GLOBAL_HEADER_LEN..dir_end];
    let directory_crc = checksum(directory);
    if directory_crc != directory_crc_stored {
        return Err(Error::new(
            ErrorKind::DirectoryCrcMismatch,
            format!("directory CRC {directory_crc:#010x} != stored {directory_crc_stored:#010x}"),
        ));
    }

    let mut blocks = Vec::with_capacity(block_count);
    let mut seen_ids = std::collections::HashSet::with_capacity(block_count);
    let mut expected_off = dir_end as u64;
    let file_len = bytes.len() as u64;

    for i in 0..block_count {
        let raw = &directory[i * DIR_ENTRY_LEN..(i + 1) * DIR_ENTRY_LEN];
        let e = DirEntry::read(raw);

        if !seen_ids.insert(e.block_id) {
            return Err(Error::new(
                ErrorKind::DuplicateBlockId,
                format!("block id {} appears more than once", e.block_id),
            ));
        }
        if e.payload_off != expected_off {
            return Err(Error::new(
                ErrorKind::PayloadOverlap,
                format!(
                    "block {} payload_off {} is not contiguous (expected {expected_off})",
                    e.block_id, e.payload_off
                ),
            ));
        }
        let end = e
            .payload_off
            .checked_add(e.payload_len as u64)
            .ok_or_else(|| {
                Error::new(ErrorKind::PayloadOutOfBounds, "payload end overflows u64")
            })?;
        if end > file_len {
            return Err(Error::new(
                ErrorKind::PayloadOutOfBounds,
                format!(
                    "block {} payload [{}, {}) lies outside a {file_len}-byte file",
                    e.block_id, e.payload_off, end
                ),
            ));
        }

        let payload = &bytes[e.payload_off as usize..end as usize];
        let payload_crc = checksum(payload);
        if payload_crc != e.payload_crc32 {
            return Err(Error::new(
                ErrorKind::PayloadCrcMismatch,
                format!(
                    "block {} payload-at-rest CRC {payload_crc:#010x} != stored {:#010x}",
                    e.block_id, e.payload_crc32
                ),
            ));
        }

        blocks.push(BlockMeta {
            block_id: e.block_id,
            original_len: e.original_len,
            original_crc: e.original_crc32,
            payload_len: e.payload_len,
        });
        expected_off = end;
    }

    if expected_off != file_len {
        return Err(Error::new(
            ErrorKind::TrailingGarbage,
            format!(
                "{} unexpected bytes follow the final payload",
                file_len - expected_off
            ),
        ));
    }

    Ok(ContainerInfo { version, blocks })
}

/// Fully decode and verify every block in `bytes`.
pub fn decode_container(bytes: &[u8], max_block_len: u64) -> Result<Vec<DecodedBlock>> {
    let info = inspect(bytes)?;
    let block_count = info.blocks.len();
    let dir_end = GLOBAL_HEADER_LEN + DIR_ENTRY_LEN * block_count;
    let directory = &bytes[GLOBAL_HEADER_LEN..dir_end];

    let mut out = Vec::with_capacity(block_count);
    for i in 0..block_count {
        let raw = &directory[i * DIR_ENTRY_LEN..(i + 1) * DIR_ENTRY_LEN];
        let e = DirEntry::read(raw);
        let start = e.payload_off as usize;
        let end = start + e.payload_len as usize;
        let payload = &bytes[start..end];

        let (data, decoded_crc) = decode_block(payload, e.original_len, max_block_len)?;
        if data.len() as u64 != e.original_len {
            return Err(Error::new(
                ErrorKind::LengthMismatch,
                format!(
                    "block {} decoded {} bytes, directory records {}",
                    e.block_id,
                    data.len(),
                    e.original_len
                ),
            ));
        }
        if decoded_crc != e.original_crc32 {
            return Err(Error::new(
                ErrorKind::OriginalCrcMismatch,
                format!(
                    "block {} decoded CRC {decoded_crc:#010x} != stored {:#010x}",
                    e.block_id, e.original_crc32
                ),
            ));
        }
        out.push(DecodedBlock {
            meta: BlockMeta {
                block_id: e.block_id,
                original_len: e.original_len,
                original_crc: e.original_crc32,
                payload_len: e.payload_len,
            },
            data,
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIMIT: u64 = 1 << 20;

    #[test]
    fn empty_container_roundtrip() {
        let bytes = encode_container(&[], LIMIT).unwrap();
        assert_eq!(bytes.len(), GLOBAL_HEADER_LEN);
        let info = inspect(&bytes).unwrap();
        assert_eq!(info.version, VERSION);
        assert!(info.blocks.is_empty());
        assert!(decode_container(&bytes, LIMIT).unwrap().is_empty());
    }

    #[test]
    fn multi_block_roundtrip() {
        let chunks: Vec<Vec<u8>> = vec![
            b"".to_vec(),
            b"aaaa".to_vec(),
            b"the quick brown fox".to_vec(),
            vec![7u8; 500],
        ];
        let bytes = encode_container(&chunks, LIMIT).unwrap();
        let decoded = decode_container(&bytes, LIMIT).unwrap();
        assert_eq!(decoded.len(), 4);
        for (d, c) in decoded.iter().zip(chunks.iter()) {
            assert_eq!(&d.data, c);
        }
        // The repetitive block is smaller at rest.
        let info = inspect(&bytes).unwrap();
        assert!(info.blocks[3].payload_len < 500);
    }

    #[test]
    fn rejects_bad_magic() {
        let mut bytes = encode_container(&[b"hi".to_vec()], LIMIT).unwrap();
        bytes[0] ^= 0xFF;
        assert_eq!(inspect(&bytes).unwrap_err().kind(), ErrorKind::BadMagic);
    }

    #[test]
    fn rejects_unknown_version() {
        let mut bytes = encode_container(&[b"hi".to_vec()], LIMIT).unwrap();
        bytes[4] = VERSION.wrapping_add(1);
        assert_eq!(
            inspect(&bytes).unwrap_err().kind(),
            ErrorKind::UnknownVersion
        );
    }

    #[test]
    fn rejects_bad_flags() {
        let mut bytes = encode_container(&[b"hi".to_vec()], LIMIT).unwrap();
        bytes[5] = 0x80;
        assert_eq!(inspect(&bytes).unwrap_err().kind(), ErrorKind::BadFlags);
    }

    #[test]
    fn detects_directory_corruption() {
        let mut bytes = encode_container(&[b"hi".to_vec()], LIMIT).unwrap();
        bytes[GLOBAL_HEADER_LEN + 5] ^= 0x01;
        assert_eq!(
            inspect(&bytes).unwrap_err().kind(),
            ErrorKind::DirectoryCrcMismatch
        );
    }

    #[test]
    fn detects_payload_corruption() {
        let mut bytes = encode_container(&[b"hello world".to_vec()], LIMIT).unwrap();
        let last = bytes.len() - 1;
        bytes[last] ^= 0x01;
        assert_eq!(
            inspect(&bytes).unwrap_err().kind(),
            ErrorKind::PayloadCrcMismatch
        );
    }

    #[test]
    fn detects_trailing_garbage() {
        let mut bytes = encode_container(&[b"hi".to_vec()], LIMIT).unwrap();
        bytes.push(0);
        assert_eq!(
            inspect(&bytes).unwrap_err().kind(),
            ErrorKind::TrailingGarbage
        );
    }

    #[test]
    fn detects_truncated_file() {
        let bytes = encode_container(&[b"hello world".to_vec()], LIMIT).unwrap();
        let cut = &bytes[..bytes.len() - 2];
        assert!(matches!(
            inspect(cut).unwrap_err().kind(),
            ErrorKind::PayloadOutOfBounds | ErrorKind::PayloadCrcMismatch
        ));
    }

    #[test]
    fn rejects_duplicate_block_ids() {
        let mut bytes = encode_container(&[b"a".to_vec(), b"b".to_vec()], LIMIT).unwrap();
        // Second entry's block_id sits at global 16 + 32.
        let pos = GLOBAL_HEADER_LEN + DIR_ENTRY_LEN;
        bytes[pos..pos + 4].copy_from_slice(&0u32.to_be_bytes());
        let dir = &mut bytes[GLOBAL_HEADER_LEN..GLOBAL_HEADER_LEN + 2 * DIR_ENTRY_LEN];
        let crc = checksum(dir);
        bytes[12..16].copy_from_slice(&crc.to_be_bytes());
        assert_eq!(
            inspect(&bytes).unwrap_err().kind(),
            ErrorKind::DuplicateBlockId
        );
    }

    #[test]
    fn truncated_header_detected() {
        assert_eq!(
            inspect(&[1, 2, 3]).unwrap_err().kind(),
            ErrorKind::TruncatedHeader
        );
    }

    #[test]
    fn original_crc_catches_semantic_tamper() {
        // Tamper directory original_len but keep payload intact: the decoded
        // bytes cannot match, surfacing a length/CRC category.
        let mut bytes = encode_container(&[b"hello".to_vec()], LIMIT).unwrap();
        let len_pos = GLOBAL_HEADER_LEN + 4; // original_len u64
        bytes[len_pos..len_pos + 8].copy_from_slice(&6u64.to_be_bytes());
        let dir = &mut bytes[GLOBAL_HEADER_LEN..GLOBAL_HEADER_LEN + DIR_ENTRY_LEN];
        let crc = checksum(dir);
        bytes[12..16].copy_from_slice(&crc.to_be_bytes());
        let err = decode_container(&bytes, LIMIT).unwrap_err();
        assert!(matches!(
            err.kind(),
            ErrorKind::LengthMismatch | ErrorKind::TruncatedBitstream
        ));
    }
}
