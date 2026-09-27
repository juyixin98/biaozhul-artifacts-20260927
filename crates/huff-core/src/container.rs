//! Container envelope: header, block frames, block directory, footer
//! (FORMAT.md §7).
//!
//! ```text
//! +-------------------------+  offset 0
//! | header (32 bytes)       |   magic/version/flags/block_size/original_total
//! |                         |   block_count/dir_offset/dir_len/header_crc
//! +-------------------------+  offset 32
//! | block frame 0           |   payload_len(u32) | crc32(u32) | payload
//! | block frame 1 ...       |
//! +-------------------------+  offset dir_offset
//! | directory bytes         |   16-byte entry per block:
//! |                         |   original_len(u32) payload_len(u32)
//! |                         |   frame_offset(u32) original_crc(u32)
//! +-------------------------+
//! | footer (16 bytes)       |   dir_crc(u32) dir_offset(u32)
//! |                         |   dir_len(u32) block_count(u32)
//! +-------------------------+
//! ```
//!
//! All multi-byte integers are little-endian. The directory is located
//! through the header and cross-checked by the footer; either pointer being
//! inconsistent (or any CRC failing) is a distinct error, never a success.

use crate::block::{encode_block, ParsedBlock, BODY_OFFSET};
use crate::crc::{crc32, expect_crc};
use crate::error::{HuffError, Result};
use crate::{FORMAT_VERSION, MAGIC, MAX_BLOCK_SIZE};

/// Header length in bytes.
pub const HEADER_LEN: usize = 32;
/// Fixed footer length in bytes.
pub const FOOTER_LEN: usize = 16;
/// One directory entry.
pub const DIR_ENTRY_LEN: usize = 16;
/// Bytes added around a payload by its frame.
pub const FRAME_OVERHEAD: usize = 8;

const HEADER_CRC_OFFSET: usize = 28;

/// One decoded, validated block together with its frame metadata.
#[derive(Debug, Clone)]
pub struct BlockView {
    pub original_len: u32,
    pub payload_len: u32,
    pub frame_offset: u32,
    pub original_crc: u32,
}

/// Parsed container metadata (no payload bytes retained).
#[derive(Debug, Clone)]
pub struct ContainerInfo {
    pub version: u8,
    pub flags: u8,
    pub block_size: u32,
    pub original_total: u32,
    pub block_count: u32,
    pub dir_offset: u32,
    pub dir_len: u32,
    pub blocks: Vec<BlockView>,
}

/// Split `data` into block-sized chunks and encode the whole container.
pub fn encode_container(data: &[u8], block_size: u32) -> Result<Vec<u8>> {
    if block_size == 0 || block_size > MAX_BLOCK_SIZE {
        return Err(HuffError::BadBlockSize);
    }
    let total = u32::try_from(data.len()).map_err(|_| HuffError::TotalLengthMismatch)?;

    let chunks: Vec<&[u8]> = data.chunks(block_size as usize).collect();
    // Empty input still produces one empty block so the directory is explicit.
    let chunks: Vec<&[u8]> = if chunks.is_empty() { vec![&[][..]] } else { chunks };
    let block_count = chunks.len() as u32;

    let mut frames: Vec<Vec<u8>> = Vec::with_capacity(chunks.len());
    for chunk in &chunks {
        let payload = encode_block(chunk)?;
        let payload_len = u32::try_from(payload.len()).unwrap();
        let mut frame = Vec::with_capacity(FRAME_OVERHEAD + payload.len());
        frame.extend_from_slice(&payload_len.to_le_bytes());
        frame.extend_from_slice(&crc32(&payload).to_le_bytes());
        frame.extend_from_slice(&payload);
        frames.push(frame);
    }

    let dir_offset = HEADER_LEN as u32
        + frames.iter().map(|f| f.len() as u32).sum::<u32>();
    let dir_len = block_count * DIR_ENTRY_LEN as u32;

    let mut header = Vec::with_capacity(HEADER_LEN);
    header.extend_from_slice(MAGIC);
    header.push(FORMAT_VERSION);
    header.push(0); // flags
    header.extend_from_slice(&block_size.to_le_bytes()); // offsets 6..10
    header.extend_from_slice(&total.to_le_bytes()); // 10..14
    header.extend_from_slice(&block_count.to_le_bytes()); // 14..18
    header.extend_from_slice(&dir_offset.to_le_bytes()); // 18..22
    header.extend_from_slice(&dir_len.to_le_bytes()); // 22..26
    header.extend_from_slice(&0u16.to_le_bytes()); // reserved 26..28
    debug_assert_eq!(header.len(), HEADER_CRC_OFFSET);
    header.extend_from_slice(&crc32(&header[..HEADER_CRC_OFFSET]).to_le_bytes());
    debug_assert_eq!(header.len(), HEADER_LEN);

    let mut directory = Vec::with_capacity(dir_len as usize);
    let mut cursor = HEADER_LEN as u32;
    for (chunk, frame) in chunks.iter().zip(frames.iter()) {
        let parsed = ParsedBlock::parse(&frame[FRAME_OVERHEAD..]).unwrap();
        let payload_len = (frame.len() - FRAME_OVERHEAD) as u32;
        directory.extend_from_slice(&parsed.original_len.to_le_bytes());
        directory.extend_from_slice(&payload_len.to_le_bytes());
        directory.extend_from_slice(&cursor.to_le_bytes());
        directory.extend_from_slice(&crc32(chunk).to_le_bytes());
        cursor += frame.len() as u32;
    }
    let dir_crc = crc32(&directory);

    let mut out = Vec::new();
    out.extend_from_slice(&header);
    for frame in &frames {
        out.extend_from_slice(frame);
    }
    out.extend_from_slice(&directory);
    out.extend_from_slice(&dir_crc.to_le_bytes());
    out.extend_from_slice(&dir_offset.to_le_bytes());
    out.extend_from_slice(&dir_len.to_le_bytes());
    out.extend_from_slice(&block_count.to_le_bytes());
    debug_assert_eq!(out.len() as u64, dir_offset as u64 + dir_len as u64 + FOOTER_LEN as u64);
    Ok(out)
}

fn take_u32(buf: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(buf[at..at + 4].try_into().unwrap())
}

/// Parse and fully verify the container structure *without* decoding blocks.
pub fn parse_container(file: &[u8]) -> Result<ContainerInfo> {
    if file.len() < HEADER_LEN {
        return Err(HuffError::HeaderTruncated);
    }
    if &file[0..4] != MAGIC {
        return Err(HuffError::BadMagic);
    }
    let version = file[4];
    if version != FORMAT_VERSION {
        // Unknown versions are refused explicitly — never guessed.
        return Err(HuffError::UnknownVersion);
    }
    let flags = file[5];
    if flags != 0 {
        return Err(HuffError::UnknownFlags);
    }
    let stored_header_crc = take_u32(file, HEADER_CRC_OFFSET);
    expect_crc(&file[..HEADER_CRC_OFFSET], stored_header_crc, HuffError::HeaderCrcMismatch)?;

    let block_size = take_u32(file, 6);
    if block_size == 0 || block_size > MAX_BLOCK_SIZE {
        return Err(HuffError::BadBlockSize);
    }
    let original_total = take_u32(file, 10);
    let block_count = take_u32(file, 14);
    let dir_offset = take_u32(file, 18);
    let dir_len = take_u32(file, 22);

    if file.len() < dir_offset as usize + dir_len as usize + FOOTER_LEN {
        return Err(HuffError::DirectoryTruncated);
    }
    let footer_start = dir_offset as usize + dir_len as usize;
    if file.len() != footer_start + FOOTER_LEN {
        return Err(HuffError::TrailingData);
    }
    let dir_crc = take_u32(file, footer_start);
    let f_dir_offset = take_u32(file, footer_start + 4);
    let f_dir_len = take_u32(file, footer_start + 8);
    let f_block_count = take_u32(file, footer_start + 12);
    if f_dir_offset != dir_offset || f_dir_len != dir_len || f_block_count != block_count {
        return Err(HuffError::DirectoryBoundsInvalid);
    }
    let dir_bytes = &file[dir_offset as usize..footer_start];
    expect_crc(dir_bytes, dir_crc, HuffError::DirectoryCrcMismatch)?;

    if dir_len != block_count * DIR_ENTRY_LEN as u32 {
        return Err(HuffError::DirectoryBoundsInvalid);
    }
    if block_count == 0 {
        return Err(HuffError::DirectoryBoundsInvalid);
    }

    let mut blocks = Vec::with_capacity(block_count as usize);
    let mut expected_offset = HEADER_LEN as u32;
    let mut sum_original: u64 = 0;
    for i in 0..block_count as usize {
        let e = dir_offset as usize + i * DIR_ENTRY_LEN;
        let original_len = take_u32(file, e);
        let payload_len = take_u32(file, e + 4);
        let frame_offset = take_u32(file, e + 8);
        let original_crc = take_u32(file, e + 12);

        if frame_offset != expected_offset {
            return Err(HuffError::DirectoryNotContiguous);
        }
        let frame_end = frame_offset
            .checked_add(FRAME_OVERHEAD as u32)
            .and_then(|v| v.checked_add(payload_len))
            .ok_or(HuffError::BlockFrameTruncated)?;
        if frame_end > dir_offset {
            return Err(HuffError::DirectoryBoundsInvalid);
        }
        if (frame_end as usize) > file.len() {
            return Err(HuffError::BlockFrameTruncated);
        }

        let start = frame_offset as usize;
        let stored_payload_len = take_u32(file, start);
        let stored_crc = take_u32(file, start + 4);
        if stored_payload_len != payload_len {
            return Err(HuffError::DirectoryBoundsInvalid);
        }
        let payload = &file[start + FRAME_OVERHEAD..start + FRAME_OVERHEAD + payload_len as usize];
        if payload.len() < BODY_OFFSET {
            return Err(HuffError::PayloadTruncated);
        }
        expect_crc(payload, stored_crc, HuffError::BlockCrcMismatch)?;

        sum_original += original_len as u64;
        blocks.push(BlockView {
            original_len,
            payload_len,
            frame_offset,
            original_crc,
        });
        expected_offset = frame_end;
    }

    if expected_offset != dir_offset {
        return Err(HuffError::DirectoryNotContiguous);
    }
    if sum_original != original_total as u64 {
        return Err(HuffError::TotalLengthMismatch);
    }

    Ok(ContainerInfo {
        version,
        flags,
        block_size,
        original_total,
        block_count,
        dir_offset,
        dir_len,
        blocks,
    })
}

/// Verify and decode a whole container, returning the concatenated original.
pub fn decode_container(file: &[u8]) -> Result<Vec<u8>> {
    let info = parse_container(file)?;
    let mut out = Vec::with_capacity(info.original_total as usize);
    for block in &info.blocks {
        let start = block.frame_offset as usize + FRAME_OVERHEAD;
        let payload = &file[start..start + block.payload_len as usize];
        let parsed = ParsedBlock::parse(payload)?;
        if parsed.original_len != block.original_len {
            return Err(HuffError::TotalLengthMismatch);
        }
        let decoded = parsed.decode()?;
        if crc32(&decoded) != block.original_crc {
            return Err(HuffError::BlockCrcMismatch);
        }
        out.extend_from_slice(&decoded);
    }
    if out.len() as u32 != info.original_total {
        return Err(HuffError::TotalLengthMismatch);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::DEFAULT_BLOCK_SIZE;

    #[test]
    fn empty_input_roundtrip() {
        let file = encode_container(b"", DEFAULT_BLOCK_SIZE).unwrap();
        let info = parse_container(&file).unwrap();
        assert_eq!(info.original_total, 0);
        assert_eq!(info.block_count, 1);
        assert_eq!(info.blocks[0].original_len, 0);
        assert!(decode_container(&file).unwrap().is_empty());
    }

    #[test]
    fn multi_block_roundtrip_and_directory() {
        let data = vec![0xA5u8; 3 * 16 + 7];
        let file = encode_container(&data, 16).unwrap();
        let info = parse_container(&file).unwrap();
        assert_eq!(info.block_count, 4);
        assert_eq!(info.blocks.iter().map(|b| b.original_len).sum::<u32>(), data.len() as u32);
        assert_eq!(decode_container(&file).unwrap(), data);
    }

    #[test]
    fn unknown_version_is_refused() {
        let mut file = encode_container(b"hi", DEFAULT_BLOCK_SIZE).unwrap();
        file[4] = 99;
        assert_eq!(parse_container(&file).unwrap_err(), HuffError::UnknownVersion);
    }

    #[test]
    fn header_crc_tampering_detected() {
        let mut file = encode_container(b"hello", DEFAULT_BLOCK_SIZE).unwrap();
        file[6] ^= 0xFF; // block_size low byte
        assert_eq!(parse_container(&file).unwrap_err(), HuffError::HeaderCrcMismatch);
    }

    #[test]
    fn directory_crc_tampering_detected() {
        let file = encode_container(b"directory tamper test data", 8).unwrap();
        let info = parse_container(&file).unwrap();
        let mut tampered = file.clone();
        let entry = info.dir_offset as usize;
        tampered[entry] ^= 0xFF; // corrupt original_len of first entry
        assert_eq!(
            parse_container(&tampered).unwrap_err(),
            HuffError::DirectoryCrcMismatch
        );
    }

    #[test]
    fn trailing_bytes_rejected() {
        let mut file = encode_container(b"trailing", DEFAULT_BLOCK_SIZE).unwrap();
        file.push(0);
        assert_eq!(parse_container(&file).unwrap_err(), HuffError::TrailingData);
    }
}
