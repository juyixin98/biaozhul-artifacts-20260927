//! Multi-step validation report (FORMAT.md §8).
//!
//! Unlike the strict parser — which stops at the first error — the validation
//! endpoint runs an ordered set of checks and records every result. Structural
//! checks are all attempted (later checks are skipped when an earlier failure
//! removed their preconditions); a final semantic decode is attempted only
//! when structure is sound. The overall verdict is `ok` only if **every**
//! check passed.

use serde::{Deserialize, Serialize};

use crate::container::{decode_container, parse_container, BlockView, ContainerInfo};
use crate::crc::crc32;
use crate::error::HuffError;

/// One validation step and its judgment.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Check {
    /// Stable machine-readable check name.
    pub name: String,
    /// `pass`, `fail` or `skip`.
    pub verdict: String,
    /// Human-readable judgment basis (what was compared).
    pub detail: String,
    /// Machine error code when `verdict == "fail"`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<String>,
}

impl Check {
    fn pass(name: &str, detail: impl Into<String>) -> Self {
        Self { name: name.to_string(), verdict: "pass".to_string(), detail: detail.into(), error_code: None }
    }
    fn fail(name: &str, detail: impl Into<String>, err: HuffError) -> Self {
        Self {
            name: name.to_string(),
            verdict: "fail".to_string(),
            detail: detail.into(),
            error_code: Some(err.code().to_string()),
        }
    }
}

/// Full validation verdict.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ValidationReport {
    pub ok: bool,
    pub checks: Vec<Check>,
    pub block_count: Option<u32>,
    pub original_total: Option<u32>,
    /// Error code of the first failed check, if any.
    pub first_error: Option<String>,
}

/// Validate every block payload's per-block CRC independently.
fn check_block_payloads(file: &[u8], info: &ContainerInfo) -> Check {
    use crate::container::FRAME_OVERHEAD;
    for block in &info.blocks {
        let start = block.frame_offset as usize + FRAME_OVERHEAD;
        let payload = &file[start..start + block.payload_len as usize];
        let frame_crc = crate::crc::crc32(payload);
        // The frame stores its own CRC; compare via re-parse of frame header.
        let stored = u32::from_le_bytes(
            file[block.frame_offset as usize + 4..block.frame_offset as usize + 8]
                .try_into()
                .unwrap(),
        );
        if frame_crc != stored {
            return Check::fail(
                "block_crc",
                format!("block @{} payload CRC mismatch", block.frame_offset),
                HuffError::BlockCrcMismatch,
            );
        }
    }
    Check::pass(
        "block_crc",
        format!("all {} block payload CRCs matched", info.blocks.len()),
    )
}

/// Decode every block and verify its declared original CRC.
fn check_semantic(file: &[u8], info: &ContainerInfo) -> Check {
    use crate::block::ParsedBlock;
    use crate::container::FRAME_OVERHEAD;
    let mut total: u64 = 0;
    for block in &info.blocks {
        let start = block.frame_offset as usize + FRAME_OVERHEAD;
        let payload = &file[start..start + block.payload_len as usize];
        let decoded = match ParsedBlock::parse(payload).and_then(|p| p.decode()) {
            Ok(d) => d,
            Err(e) => {
                return Check::fail(
                    "semantic_decode",
                    format!("block @{} failed: {}", block.frame_offset, e.code()),
                    e,
                )
            }
        };
        if decoded.len() as u32 != block.original_len {
            return Check::fail(
                "semantic_decode",
                format!(
                    "block @{} decoded length {} != declared {}",
                    block.frame_offset,
                    decoded.len(),
                    block.original_len
                ),
                HuffError::OutputLengthMismatch,
            );
        }
        if crc32(&decoded) != block.original_crc {
            return Check::fail(
                "original_crc",
                format!("block @{} decoded data CRC mismatch", block.frame_offset),
                HuffError::BlockCrcMismatch,
            );
        }
        total += decoded.len() as u64;
    }
    if total != info.original_total as u64 {
        return Check::fail(
            "total_length",
            format!("decoded total {total} != header total {}", info.original_total),
            HuffError::TotalLengthMismatch,
        );
    }
    Check::pass(
        "semantic_decode",
        format!("decoded {} blocks, {} bytes total", info.block_count, total),
    )
}

/// Run all validation checks against a candidate container.
pub fn validate(file: &[u8]) -> ValidationReport {
    let mut checks = Vec::new();

    // 1. Minimum size for a header.
    if file.len() < crate::container::HEADER_LEN {
        checks.push(Check::fail(
            "header_present",
            format!("file is {} bytes, need at least {}", file.len(), crate::container::HEADER_LEN),
            HuffError::HeaderTruncated,
        ));
        return finish(checks, None, None);
    }
    checks.push(Check::pass("header_present", "32-byte header present"));

    // 2. Magic.
    if &file[0..4] != crate::MAGIC {
        checks.push(Check::fail(
            "magic",
            format!("magic bytes {:02X?} != {:02X?}", &file[0..4], crate::MAGIC),
            HuffError::BadMagic,
        ));
        return finish(checks, None, None);
    }
    checks.push(Check::pass("magic", "magic = HUFF"));

    // 3. Version — unknown versions refused, not guessed.
    let version = file[4];
    if version != crate::FORMAT_VERSION {
        checks.push(Check::fail(
            "version",
            format!("container version {version} is not supported (want {})", crate::FORMAT_VERSION),
            HuffError::UnknownVersion,
        ));
        return finish(checks, None, None);
    }
    checks.push(Check::pass("version", format!("version = {version}")));

    // 4. Flags.
    let flags = file[5];
    if flags != 0 {
        checks.push(Check::fail(
            "flags",
            format!("reserved flags byte = 0x{flags:02X}, expected 0x00"),
            HuffError::UnknownFlags,
        ));
        return finish(checks, None, None);
    }
    checks.push(Check::pass("flags", "reserved flags = 0"));

    // 5. Header CRC.
    let header_crc = u32::from_le_bytes(file[28..32].try_into().unwrap());
    if crc32(&file[..28]) != header_crc {
        checks.push(Check::fail(
            "header_crc",
            "CRC-32 of the first 28 header bytes differs from the stored value",
            HuffError::HeaderCrcMismatch,
        ));
        return finish(checks, None, None);
    }
    checks.push(Check::pass("header_crc", "header CRC-32 matched"));

    // 6. Structural parse (directory bounds, contiguity, totals).
    let info = match parse_container(file) {
        Ok(info) => {
            checks.push(Check::pass(
                "structure",
                format!(
                    "{} blocks, directory @{} len {}, {} original bytes",
                    info.block_count, info.dir_offset, info.dir_len, info.original_total
                ),
            ));
            info
        }
        Err(e) => {
            checks.push(Check::fail(
                "structure",
                format!("structural parse rejected: {}", e.code()),
                e,
            ));
            return finish(checks, None, None);
        }
    };

    // 7. Per-payload CRCs (parse_container already verified them, but record
    //    the check explicitly with per-block judgment basis).
    checks.push(check_block_payloads(file, &info));

    // 8. Semantic decode of every block + original CRC.
    checks.push(check_semantic(file, &info));

    let bc = info.block_count;
    let total = info.original_total;
    finish(checks, Some(bc), Some(total))
}

fn finish(checks: Vec<Check>, block_count: Option<u32>, original_total: Option<u32>) -> ValidationReport {
    let first_error = checks
        .iter()
        .find(|c| c.verdict == "fail")
        .and_then(|c| c.error_code.clone());
    ValidationReport {
        ok: first_error.is_none(),
        checks,
        block_count,
        original_total,
        first_error,
    }
}

/// Decode and return the original bytes (used by the decode endpoint).
pub fn decode(file: &[u8]) -> Result<Vec<u8>, HuffError> {
    decode_container(file)
}

/// Kept so the module documents block-level fields for callers.
pub type Block = BlockView;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::container::encode_container;
    use crate::DEFAULT_BLOCK_SIZE;

    #[test]
    fn valid_container_report_is_all_pass() {
        let file = encode_container(b"reportable data here", DEFAULT_BLOCK_SIZE).unwrap();
        let report = validate(&file);
        assert!(report.ok, "failing checks: {:?}", report.checks);
        assert!(report.checks.iter().all(|c| c.verdict == "pass"));
    }

    #[test]
    fn unknown_version_report_is_failure_not_success() {
        let mut file = encode_container(b"x", DEFAULT_BLOCK_SIZE).unwrap();
        file[4] = 7;
        let report = validate(&file);
        assert!(!report.ok);
        assert_eq!(report.first_error.as_deref(), Some("UNKNOWN_VERSION"));
    }

    #[test]
    fn bad_magic_report() {
        let mut file = encode_container(b"x", DEFAULT_BLOCK_SIZE).unwrap();
        file[0] = b'X';
        let report = validate(&file);
        assert!(!report.ok);
        assert_eq!(report.first_error.as_deref(), Some("BAD_MAGIC"));
    }

    #[test]
    fn truncated_file_report() {
        let file = encode_container(b"x", DEFAULT_BLOCK_SIZE).unwrap();
        let report = validate(&file[..10]);
        assert!(!report.ok);
        assert_eq!(report.first_error.as_deref(), Some("HEADER_TRUNCATED"));
    }
}
