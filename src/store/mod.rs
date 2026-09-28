//! Filesystem persistence adapter.
//!
//! Layout under a root directory:
//!
//! ```text
//! <root>/<stream-id>/block-00000000.lzb
//! <root>/<stream-id>/block-00000001.lzb
//! ```
//!
//! Stream ids are restricted to a conservative character set so they can only
//! name entries inside the store (no path separators, no `..`). Block files are
//! written atomically (`tmp` + `rename`) and never overwritten. Every byte on
//! disk counts against an aggregate store cap, enforced before write.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

use crate::core::constants::{
    BLOCK_PREFIX, BLOCK_SUFFIX, DEFAULT_STORE_CAP, DEFAULT_STREAM_OUTPUT_CAP,
};
use crate::core::decoder::ChainSession;
use crate::core::error::{Category, Code, Error, Result};
use crate::core::format::BlockHeader;

/// Validate and normalize a caller-supplied stream id.
pub fn validate_stream_id(id: &str) -> Result<()> {
    // `.` and `..` survive the allowlist below but must never name a stream.
    if id == "." || id == ".." {
        return Err(Error::new(
            Code::BadString,
            "stream id must not be '.' or '..'",
        ));
    }
    let ok = id.len() <= 64
        && !id.is_empty()
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'));
    if !ok {
        return Err(Error::new(
            Code::BadString,
            "stream id must be 1-64 chars of [A-Za-z0-9-_.] and not '.' or '..'",
        ));
    }
    Ok(())
}

fn block_file_name(index: u32) -> String {
    format!("{BLOCK_PREFIX}{:08}{BLOCK_SUFFIX}", index)
}

/// One recorded block entry.
#[derive(Debug, Clone)]
pub struct StoredBlock {
    pub index: u32,
    pub path: PathBuf,
    pub size: u64,
}

/// Stream index held in memory while the store is open; files remain the
/// durable record and are rescanned on open.
#[derive(Debug, Default)]
struct StreamIndex {
    blocks: Vec<StoredBlock>,
    total: u64,
}

#[derive(Debug)]
pub struct BlockStore {
    root: PathBuf,
    cap_bytes: u64,
    stream_output_cap: u64,
    inner: Mutex<StoreInner>,
}

#[derive(Debug, Default)]
struct StoreInner {
    streams: std::collections::BTreeMap<String, StreamIndex>,
    bytes_on_disk: u64,
}

impl BlockStore {
    /// Open (creating if needed) a store at `root`, re-indexing existing files.
    pub fn open(root: impl Into<PathBuf>) -> Result<Self> {
        Self::open_with_cap(root, DEFAULT_STORE_CAP)
    }

    pub fn open_with_cap(root: impl Into<PathBuf>, cap_bytes: u64) -> Result<Self> {
        Self::open_with_caps(root, cap_bytes, DEFAULT_STREAM_OUTPUT_CAP)
    }

    /// Open with both an on-disk byte cap and a per-stream decompressed cap.
    pub fn open_with_caps(
        root: impl Into<PathBuf>,
        cap_bytes: u64,
        stream_output_cap: u64,
    ) -> Result<Self> {
        let root = root.into();
        fs::create_dir_all(&root).map_err(|e| {
            Error::io(
                crate::core::error::Source::Store,
                format!("create store root: {e}"),
            )
        })?;
        let mut store = BlockStore {
            root: root.clone(),
            cap_bytes,
            stream_output_cap,
            inner: Mutex::new(StoreInner::default()),
        };
        store.rescan()?;
        Ok(store)
    }

    fn lock(&self) -> MutexGuard<'_, StoreInner> {
        self.inner.lock().expect("store mutex poisoned")
    }

    fn stream_dir(&self, id: &str) -> PathBuf {
        self.root.join(id)
    }

    fn block_path(&self, id: &str, index: u32) -> PathBuf {
        self.stream_dir(id).join(block_file_name(index))
    }

    /// Rebuild in-memory indexes from the directory tree.
    fn rescan(&mut self) -> Result<()> {
        let mut inner = self.lock();
        inner.streams.clear();
        inner.bytes_on_disk = 0;

        let entries = fs::read_dir(&self.root)
            .map_err(|e| Error::io(crate::core::error::Source::Store, format!("read root: {e}")))?;
        for entry in entries {
            let entry = entry.map_err(|e| {
                Error::io(crate::core::error::Source::Store, format!("dir entry: {e}"))
            })?;
            if !entry.file_type().map_err(io_dir)?.is_dir() {
                continue;
            }
            let id = entry.file_name().to_string_lossy().to_string();
            if validate_stream_id(&id).is_err() {
                continue; // ignore foreign entries, never touch them
            }
            let mut index = StreamIndex::default();
            for blk in fs::read_dir(entry.path()).map_err(io_dir)? {
                let blk = blk.map_err(io_dir)?;
                // read_dir file types are not symlink-followed: index only real
                // regular files, never a planted symlink.
                if !blk.file_type().map_err(io_dir)?.is_file() {
                    continue;
                }
                let name = blk.file_name().to_string_lossy().to_string();
                let Some(rest) = name.strip_prefix(BLOCK_PREFIX) else {
                    continue;
                };
                let Some(num) = rest.strip_suffix(BLOCK_SUFFIX) else {
                    continue;
                };
                let Ok(n) = num.parse::<u32>() else { continue };
                // symlink_metadata does not follow a final symlink either.
                let size = fs::symlink_metadata(blk.path()).map_err(io_dir)?.len();
                index.blocks.push(StoredBlock {
                    index: n,
                    path: blk.path(),
                    size,
                });
                index.total += size;
                inner.bytes_on_disk += size;
            }
            index.blocks.sort_by_key(|b| b.index);
            inner.streams.insert(id, index);
        }
        Ok(())
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Total bytes occupied by block files.
    pub fn bytes_on_disk(&self) -> u64 {
        self.lock().bytes_on_disk
    }

    pub fn cap_bytes(&self) -> u64 {
        self.cap_bytes
    }

    pub fn list_streams(&self) -> Vec<String> {
        self.lock().streams.keys().cloned().collect()
    }

    pub fn stream_len(&self, id: &str) -> Result<u64> {
        validate_stream_id(id)?;
        let inner = self.lock();
        Ok(inner.streams.get(id).map_or(0, |s| s.total))
    }

    pub fn block_count(&self, id: &str) -> Result<usize> {
        validate_stream_id(id)?;
        Ok(self.lock().streams.get(id).map_or(0, |s| s.blocks.len()))
    }

    /// Read one block file.
    pub fn read_block(&self, id: &str, index: u32) -> Result<Vec<u8>> {
        validate_stream_id(id)?;
        let path = {
            let inner = self.lock();
            let stream = inner
                .streams
                .get(id)
                .ok_or_else(|| Error::new(Code::NotFound, format!("stream {id} not found")))?;
            stream
                .blocks
                .iter()
                .find(|b| b.index == index)
                .ok_or_else(|| {
                    Error::new(Code::NotFound, format!("block {index} not found in {id}"))
                })?
                .path
                .clone()
        };
        fs::read(&path).map_err(|e| {
            Error::io(
                crate::core::error::Source::BlockFile,
                format!("read {}: {e}", path.display()),
            )
        })
    }

    /// Persist a new block. Enforces:
    ///
    /// * valid stream id,
    /// * consistent index (next in sequence),
    /// * no overwrite,
    /// * aggregate byte cap *before* writing.
    ///
    /// Header bytes are parsed so the recorded index matches the wire index.
    pub fn append_block(&self, id: &str, raw: &[u8]) -> Result<StoredBlock> {
        validate_stream_id(id)?;
        let header = BlockHeader::decode(raw)?;

        let mut inner = self.lock();
        let expected = inner
            .streams
            .get(id)
            .map_or(0u32, |s| s.blocks.last().map_or(0, |b| b.index + 1));
        if header.index != expected {
            return Err(Error::new(
                Code::IndexGap,
                format!(
                    "wire index {} not contiguous (expected {expected})",
                    header.index
                ),
            ));
        }
        let dir = self.stream_dir(id);
        let path = self.block_path(id, header.index);
        // Never write through a planted symlink for either the stream directory
        // or the block file (fail closed; this is a local-tamper signal).
        if is_symlink(&dir) || is_symlink(&path) {
            return Err(Error::io(
                crate::core::error::Source::Store,
                format!(
                    "refusing to write through a symlink near {}",
                    path.display()
                ),
            ));
        }
        if fs::symlink_metadata(&path).is_ok() {
            return Err(Error::new(
                Code::AlreadyExists,
                format!("{} already exists", path.display()),
            ));
        }
        let size = raw.len() as u64;
        if inner.bytes_on_disk.saturating_add(size) > self.cap_bytes {
            return Err(Error::new(
                Code::TotalCapExceeded,
                format!(
                    "storing {size} bytes would exceed store cap {} (used {})",
                    self.cap_bytes, inner.bytes_on_disk
                ),
            ));
        }

        fs::create_dir_all(&dir).map_err(io_dir)?;
        // Re-check after creation: create_dir_all follows an existing symlink dir.
        if is_symlink(&dir) {
            return Err(Error::io(
                crate::core::error::Source::Store,
                format!("stream directory {} is a symlink", dir.display()),
            ));
        }
        atomic_write(&path, raw)?;

        let record = StoredBlock {
            index: header.index,
            path: path.clone(),
            size,
        };
        let stream = inner.streams.entry(id.to_string()).or_default();
        stream.blocks.push(record.clone());
        stream.total += size;
        inner.bytes_on_disk += size;
        Ok(record)
    }

    /// Aggregate cap on a stream's decompressed bytes.
    pub fn stream_output_cap(&self) -> u64 {
        self.stream_output_cap
    }

    /// Decode one stream completely from disk using a fresh chain session.
    ///
    /// The running output is bounded by the per-stream decompressed cap: each
    /// block's declared length is checked against the *remaining* budget
    /// before that block is decoded, so a chain of high-ratio "bomb" blocks
    /// cannot materialize unbounded memory.
    pub fn decode_stream(&self, id: &str) -> Result<Vec<u8>> {
        validate_stream_id(id)?;
        let paths: Vec<PathBuf> = {
            let inner = self.lock();
            let stream = inner
                .streams
                .get(id)
                .ok_or_else(|| Error::new(Code::NotFound, format!("stream {id} not found")))?;
            stream.blocks.iter().map(|b| b.path.clone()).collect()
        };
        let mut session = ChainSession::new();
        let mut out: Vec<u8> = Vec::new();
        for path in paths {
            // Pre-check the next block's declared length against the remaining
            // stream budget before reading/decoding it.
            let raw = fs::read(&path).map_err(|e| {
                Error::io(
                    crate::core::error::Source::BlockFile,
                    format!("read {}: {e}", path.display()),
                )
            })?;
            let header = BlockHeader::decode(&raw)?;
            let new_total = (out.len() as u64)
                .checked_add(header.decompressed_len)
                .ok_or_else(|| {
                    Error::new(
                        Code::StreamOutputCapExceeded,
                        "stream length arithmetic overflow",
                    )
                })?;
            if new_total > self.stream_output_cap {
                return Err(Error::new(
                    Code::StreamOutputCapExceeded,
                    format!(
                        "decoding stream {id} would reach {new_total} bytes, cap {}",
                        self.stream_output_cap
                    ),
                ));
            }
            let part = session.decode_raw(&raw)?;
            out.extend_from_slice(&part);
        }
        Ok(out)
    }
}

fn io_dir(e: std::io::Error) -> Error {
    Error::io(
        crate::core::error::Source::Store,
        format!("directory scan: {e}"),
    )
}

/// True if `p` exists and is a symbolic link (uses non-following metadata).
fn is_symlink(p: &Path) -> bool {
    fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_symlink())
}

/// Create a new regular file, refusing an existing path and refusing to open
/// through a final symlink (`O_NOFOLLOW | O_EXCL` on Unix).
fn create_new_nofollow(p: &Path) -> std::io::Result<fs::File> {
    use std::fs::OpenOptions;
    let mut opts = OpenOptions::new();
    opts.write(true).create_new(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        // O_NOFOLLOW fails with ELOOP if the final component is a symlink.
        opts.custom_flags(libc_o_no_follow());
    }
    opts.open(p)
}

#[cfg(unix)]
fn libc_o_no_follow() -> i32 {
    // libc::O_NOFOLLOW without taking a libc dependency.
    if cfg!(target_os = "linux") {
        0o400000
    } else if cfg!(any(
        target_os = "macos",
        target_os = "freebsd",
        target_os = "openbsd",
        target_os = "netbsd"
    )) {
        0o100
    } else {
        0
    }
}

/// Write via a temp file in the same directory, then atomically rename.
fn atomic_write(path: &Path, data: &[u8]) -> Result<()> {
    let tmp = path.with_extension("lzb.tmp");
    // Remove any stale temp from a previous crashed write, but only a real
    // regular file — never through a symlink.
    if let Ok(meta) = fs::symlink_metadata(&tmp) {
        if meta.file_type().is_symlink() {
            return Err(Error::io(
                crate::core::error::Source::BlockFile,
                format!("refusing to overwrite symlink {}", tmp.display()),
            ));
        }
        let _ = fs::remove_file(&tmp);
    }
    {
        use std::io::Write;
        let mut f = create_new_nofollow(&tmp).map_err(|e| {
            Error::io(
                crate::core::error::Source::BlockFile,
                format!("write {}: {e}", tmp.display()),
            )
        })?;
        f.write_all(data).map_err(|e| {
            Error::io(
                crate::core::error::Source::BlockFile,
                format!("write {}: {e}", tmp.display()),
            )
        })?;
        f.sync_all().ok();
    }
    fs::rename(&tmp, path).map_err(|e| {
        // Do not leak the temp file on a failed rename.
        let _ = fs::remove_file(&tmp);
        Error::io(
            crate::core::error::Source::BlockFile,
            format!("rename: {e}"),
        )
    })
}

/// Every error leaving the store carries a known category.
pub fn assert_category_contract(e: &Error) -> Category {
    e.category()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::decoder::encode_next;
    use crate::core::decoder::ChainSession;

    fn tempdir(tag: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "lz77b-test-{}-{}-{tag}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn stream_id_is_path_safe() {
        assert!(validate_stream_id("ok-id_1.2").is_ok());
        assert!(validate_stream_id("../escape").is_err());
        assert!(validate_stream_id("a/b").is_err());
        assert!(validate_stream_id("").is_err());
        assert!(validate_stream_id(&"x".repeat(65)).is_err());
    }

    #[test]
    fn append_and_decode_chain_persists() {
        let dir = tempdir("chain");
        let store = BlockStore::open(&dir).unwrap();
        let mut session = ChainSession::new();
        for chunk in [b"hello world hello world".as_slice(), b"hello again"] {
            let out = encode_next(&session, chunk).unwrap();
            store.append_block("s1", &out.raw).unwrap();
            session.decode_raw(&out.raw).unwrap();
        }
        assert_eq!(store.block_count("s1").unwrap(), 2);
        let decoded = store.decode_stream("s1").unwrap();
        assert_eq!(decoded, b"hello world hello worldhello again");

        // Reopening rescans disk.
        drop(store);
        let reopened = BlockStore::open(&dir).unwrap();
        assert_eq!(reopened.decode_stream("s1").unwrap().len(), decoded.len());
    }

    #[test]
    fn rejects_index_gap_and_overwrite() {
        let dir = tempdir("gap");
        let store = BlockStore::open(&dir).unwrap();
        let b0 = {
            let s = ChainSession::new();
            encode_next(&s, b"abcabcabc").unwrap().raw
        };
        let b1 = {
            let mut s = ChainSession::new();
            let o0 = encode_next(&s, b"abcabcabc").unwrap();
            s.decode_raw(&o0.raw).unwrap();
            encode_next(&s, b"abcabc").unwrap().raw
        };
        store.append_block("s", &b1).unwrap_err(); // starts at 1 -> gap
        assert_eq!(
            store.append_block("s", &b1).unwrap_err().code,
            Code::IndexGap
        );
        store.append_block("s", &b0).unwrap();
        assert_eq!(
            store.append_block("s", &b0).unwrap_err().code,
            Code::IndexGap
        ); // duplicate index
    }

    #[test]
    fn total_cap_is_enforced_before_write() {
        let dir = tempdir("cap");
        let store = BlockStore::open_with_cap(&dir, 32).unwrap();
        let s = ChainSession::new();
        let b0 = encode_next(&s, b"abcabcabcabcabcabc").unwrap().raw;
        assert!(b0.len() > 32, "test block must exceed the tiny cap");
        let err = store.append_block("s", &b0).unwrap_err();
        assert_eq!(err.code, Code::TotalCapExceeded);
        assert_eq!(err.category(), Category::Resource);
        assert_eq!(store.block_count("s").unwrap_or(0), 0);
        // Nothing was left on disk after the refused write.
        assert!(store.decode_stream("s").is_err() || store.stream_len("s").unwrap_or(1) == 0);
    }

    #[test]
    fn block_that_fits_is_stored_even_under_small_cap() {
        let dir = tempdir("capfit");
        let store = BlockStore::open_with_cap(&dir, 64 * 1024).unwrap();
        let s = ChainSession::new();
        let b0 = encode_next(&s, b"abcabcabcabcabcabc").unwrap().raw;
        assert!(store.append_block("s", &b0).is_ok());
    }
}
