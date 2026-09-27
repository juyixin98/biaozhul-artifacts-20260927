//! On-disk format: versioned binary container with checksum.
//!
//! Layout (all multi-byte integers little-endian):
//!
//! ```text
//! offset  size  field
//! 0       4     magic = b"MPHF"
//! 4       1     FORMAT_VERSION (currently 1)
//! 5       1     algorithm id (1 = BDZ 3-hypergraph peeling)
//! 6       1     verifier bits: 0 = full key, else 8/16/32/64 fingerprint
//! 7       1     reserved (must be 0)
//! 8       8     n  (number of keys = peel vertices)
//! 16      8     m  (number of hypergraph vertices)
//! 24      8     seed
//! 32      8     data_len (bytes in body)
//! 40      4     CRC-32 (IEEE) over header[8..40] concatenated with body
//! 44      4     reserved (must be 0)
//! 48      ...   body:
//!               g_packed           : ceil(m/4) bytes (2 bits per vertex)
//!               occ_packed         : ceil(m/8) bytes (occupancy bitset)
//!               fingerprints       : n * bits/8 bytes           [fp mode]
//!               keys_blob          : remaining - 8*(n+1) bytes  [full mode]
//!               key_offsets        : (n+1) little-endian u64    [full mode]
//! ```
//!
//! The format binds **hash seed, algorithm and format version**: a loader
//! refuses anything whose magic/version/algorithm it does not understand,
//! and verifies the checksum before trusting any field.

use std::io::{Read, Write};
use std::path::Path;

use crc32fast::Hasher;

use crate::error::{ErrorKind, MphfError, Result};
use crate::index::{MphfIndex, VerifyMode};

pub const MAGIC: &[u8; 4] = b"MPHF";
pub const FORMAT_VERSION: u8 = 1;
/// Algorithm id: BDZ-style 3-uniform hypergraph peeling.
pub const ALGO_BDZ3: u8 = 1;
const HEADER_LEN: usize = 48;

fn put_u64(buf: &mut [u8], off: usize, v: u64) {
    buf[off..off + 8].copy_from_slice(&v.to_le_bytes());
}

fn read_u64(buf: &[u8], off: usize) -> u64 {
    u64::from_le_bytes(buf[off..off + 8].try_into().unwrap())
}

/// Encode body only (returned bytes exclude the header).
fn encode_body(idx: &MphfIndex) -> Vec<u8> {
    let mut body = Vec::new();
    body.extend_from_slice(idx.g_packed());
    body.extend_from_slice(idx.occ_packed());
    match idx.mode {
        VerifyMode::Fingerprint { .. } => {
            body.extend_from_slice(idx.fingerprints());
        }
        VerifyMode::FullKey => {
            body.extend_from_slice(idx.keys_blob());
            for o in idx.key_offsets() {
                body.extend_from_slice(&o.to_le_bytes());
            }
        }
    }
    body
}

/// Serialize to a writer and return the CRC that was stored.
pub fn write<W: Write>(w: &mut W, idx: &MphfIndex) -> Result<u32> {
    let mut header = vec![0u8; HEADER_LEN];
    header[0..4].copy_from_slice(MAGIC);
    header[4] = FORMAT_VERSION;
    header[5] = idx.algo;
    header[6] = idx.mode.bits();
    // header[7] reserved
    put_u64(&mut header, 8, idx.n as u64);
    put_u64(&mut header, 16, idx.m as u64);
    put_u64(&mut header, 24, idx.seed);
    let body = encode_body(idx);
    put_u64(&mut header, 32, body.len() as u64);

    let mut hasher = Hasher::new();
    hasher.update(&header[8..40]);
    hasher.update(&body);
    let crc = hasher.finalize();
    header[40..44].copy_from_slice(&crc.to_le_bytes());

    w.write_all(&header)?;
    w.write_all(&body)?;
    w.flush()?;
    Ok(crc)
}

/// Atomically persist: write `<path>.tmp` then rename, so a reader never
/// observes a half-written index.
pub fn save_to_path(path: impl AsRef<Path>, idx: &MphfIndex) -> Result<()> {
    let path = path.as_ref();
    let tmp = path.with_extension("mphf.tmp");
    {
        let mut f = std::fs::File::create(&tmp)?;
        write(&mut f, idx)?;
    }
    std::fs::rename(&tmp, path)?;
    Ok(())
}

struct Decoded {
    algo: u8,
    mode: VerifyMode,
    n: usize,
    m: usize,
    seed: u64,
    g_packed: Vec<u8>,
    occ_packed: Vec<u8>,
    fps: Vec<u8>,
    keys_blob: Vec<u8>,
    key_offsets: Vec<u64>,
}

fn decode(header: &[u8], body: &[u8]) -> Result<Decoded> {
    if header.len() != HEADER_LEN {
        return Err(MphfError::format(
            ErrorKind::FormatCorrupt,
            "short header",
        ));
    }
    if &header[0..4] != MAGIC {
        return Err(MphfError::format(
            ErrorKind::FormatMagic,
            "bad magic bytes; not an MPHF index",
        ));
    }
    let _version = header[4];
    if _version != FORMAT_VERSION {
        return Err(MphfError::format(
            ErrorKind::FormatVersion,
            format!("unsupported format version {_version}, expected {FORMAT_VERSION}"),
        ));
    }
    let algo = header[5];
    if algo != ALGO_BDZ3 {
        return Err(MphfError::format(
            ErrorKind::FormatVersion,
            format!("unsupported algorithm id {algo}"),
        ));
    }
    if header[7] != 0 {
        return Err(MphfError::format(
            ErrorKind::FormatCorrupt,
            "reserved header byte 7 is non-zero",
        ));
    }
    let mode = VerifyMode::parse(header[6]).map_err(|_| {
        MphfError::format(
            ErrorKind::FormatCorrupt,
            format!("invalid verifier bits {}", header[6]),
        )
    })?;
    let n = read_u64(header, 8) as usize;
    let m = read_u64(header, 16) as usize;
    let seed = read_u64(header, 24);
    let data_len = read_u64(header, 32) as usize;
    if data_len != body.len() {
        return Err(MphfError::format(
            ErrorKind::FormatCorrupt,
            format!("declared data_len {data_len} != body length {}", body.len()),
        ));
    }

    let gpk = m.div_ceil(4);
    let opk = m.div_ceil(8);
    if body.len() < gpk + opk {
        return Err(MphfError::format(
            ErrorKind::FormatCorrupt,
            "body shorter than g+occupancy tables",
        ));
    }
    let g_packed = body[..gpk].to_vec();
    let occ_packed = body[gpk..gpk + opk].to_vec();
    let rest = &body[gpk + opk..];

    let (fps, keys_blob, key_offsets) = match mode {
        VerifyMode::Fingerprint { bits } => {
            let width = (bits / 8) as usize;
            let need = n.checked_mul(width).ok_or_else(|| {
                MphfError::format(ErrorKind::FormatCorrupt, "n*width overflow")
            })?;
            if rest.len() != need {
                return Err(MphfError::format(
                    ErrorKind::FormatCorrupt,
                    format!("fingerprint table {} bytes != expected {need}", rest.len()),
                ));
            }
            (rest.to_vec(), Vec::new(), Vec::new())
        }
        VerifyMode::FullKey => {
            if n > usize::MAX / 8 || rest.len() < 8 * (n + 1) {
                return Err(MphfError::format(
                    ErrorKind::FormatCorrupt,
                    "body shorter than key offset table",
                ));
            }
            let blob_len = rest.len() - 8 * (n + 1);
            let blob = rest[..blob_len].to_vec();
            let mut offsets = Vec::with_capacity(n + 1);
            for i in 0..n + 1 {
                offsets.push(read_u64(rest, blob_len + i * 8));
            }
            if offsets[0] != 0 || *offsets.last().unwrap() as usize != blob.len() {
                return Err(MphfError::format(
                    ErrorKind::FormatCorrupt,
                    "key offset table does not bracket key blob",
                ));
            }
            for w in offsets.windows(2) {
                if w[1] < w[0] {
                    return Err(MphfError::format(
                        ErrorKind::FormatCorrupt,
                        "key offsets not monotonic",
                    ));
                }
            }
            (Vec::new(), blob, offsets)
        }
    };

    // Structural cross-checks:
    //  - exactly n vertices are occupied;
    //  - occupied vertices carry g in 0..2;
    //  - unoccupied vertices must carry g=0 (normalized);
    //  - padding bits of the occupancy byte are clear.
    let mut occupied_count = 0usize;
    for v in 0..m {
        let gv = (g_packed[v / 4] >> ((v % 4) * 2)) & 3;
        let on = (occ_packed[v / 8] >> (v % 8)) & 1 == 1;
        if on {
            occupied_count += 1;
            if gv > 2 {
                return Err(MphfError::format(
                    ErrorKind::FormatCorrupt,
                    format!("occupied vertex {v} has invalid g={gv}"),
                ));
            }
        } else if gv != 0 {
            return Err(MphfError::format(
                ErrorKind::FormatCorrupt,
                format!("unoccupied vertex {v} carries non-zero g={gv}"),
            ));
        }
    }
    for v in m..opk * 8 {
        if (occ_packed[v / 8] >> (v % 8)) & 1 == 1 {
            return Err(MphfError::format(
                ErrorKind::FormatCorrupt,
                format!("padding occupancy bit set at vertex {v}"),
            ));
        }
    }
    if occupied_count != n {
        return Err(MphfError::format(
            ErrorKind::FormatCorrupt,
            format!("occupancy has {occupied_count} vertices, expected n={n}"),
        ));
    }

    Ok(Decoded {
        algo,
        mode,
        n,
        m,
        seed,
        g_packed,
        occ_packed,
        fps,
        keys_blob,
        key_offsets,
    })
}

pub fn read<R: Read>(r: &mut R) -> Result<MphfIndex> {
    let mut header = vec![0u8; HEADER_LEN];
    r.read_exact(&mut header)?;
    let data_len = read_u64(&header, 32) as usize;
    let mut body = vec![0u8; data_len];
    r.read_exact(&mut body)?;

    let stored_crc = u32::from_le_bytes(header[40..44].try_into().unwrap());
    let mut hasher = Hasher::new();
    hasher.update(&header[8..40]);
    hasher.update(&body);
    if hasher.finalize() != stored_crc {
        return Err(MphfError::format(
            ErrorKind::ChecksumMismatch,
            "payload CRC-32 mismatch",
        ));
    }

    let d = decode(&header, &body)?;
    Ok(MphfIndex::from_parts(
        d.n,
        d.m,
        d.seed,
        d.algo,
        d.mode,
        d.g_packed,
        d.occ_packed,
        d.fps,
        d.keys_blob,
        d.key_offsets,
    ))
}

pub fn load_from_path(path: impl AsRef<Path>) -> Result<MphfIndex> {
    let mut f = std::fs::File::open(path.as_ref())?;
    read(&mut f)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_constant_stable() {
        // The wire contract: changing this invalidates old files on purpose.
        assert_eq!(MAGIC, b"MPHF");
        assert_eq!(FORMAT_VERSION, 1);
        assert_eq!(HEADER_LEN, 48);
    }
}
