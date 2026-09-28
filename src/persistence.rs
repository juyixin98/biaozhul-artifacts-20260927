//! File-system persistence adapter.
//!
//! On-disk layout (all multi-byte integers little-endian):
//!
//! ```text
//! magic        8 bytes   b"FMIDX001"
//! name_len     u32, name bytes (1..=64)
//! sample_interval u32
//! sections, in this fixed order:
//!   tag        4 bytes   b"TXT1" | b"BWT1" | b"SAS1"
//!   payload_len  u32
//!   payload_crc  u32      CRC-32 (IEEE) of the payload bytes
//!   payload      [u8]
//! TXT1 payload: raw text bytes
//! BWT1 payload: u64 n, then n * u16 symbols
//! SAS1 payload: u64 count, then count * u32 SA samples
//! ```
//!
//! A [`Catalog`] (`catalog.json`) maps index names to metadata. Writes are
//! atomic: the index file is written to `name.fm.tmp` and renamed, then the
//! catalog is rewritten and renamed, so a crash never leaves a half-index
//! referenced by the catalog.

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::{Error, Result};
use crate::fm::FmIndex;

pub const MAGIC: &[u8; 8] = b"FMIDX001";
const TAG_TXT: &[u8; 4] = b"TXT1";
const TAG_BWT: &[u8; 4] = b"BWT1";
const TAG_SAS: &[u8; 4] = b"SAS1";

/// One catalog record (JSON-serialized in `catalog.json`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IndexMeta {
    pub name: String,
    pub file: String,
    pub text_len: u64,
    pub sample_interval: u32,
    /// Lower-hex SHA-256 of the original text.
    pub sha256: String,
    pub created_unix_ms: u128,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct CatalogFile {
    version: u32,
    indexes: BTreeMap<String, IndexMeta>,
}

/// File-system-backed registry of named indexes.
pub struct Catalog {
    root: PathBuf,
    inner: CatalogFile,
}

/// Validate the public index-name rule shared by HTTP and persistence layers.
pub fn validate_name(name: &str) -> Result<()> {
    let valid_chars = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '-';
    if name.is_empty() || name.len() > 64 || !name.chars().all(valid_chars) {
        return Err(Error::BadName {
            name: name.to_string(),
            reason: "names must be 1..=64 chars of [A-Za-z0-9_-]",
        });
    }
    if name == "." || name == ".." {
        return Err(Error::BadName {
            name: name.to_string(),
            reason: "reserved name",
        });
    }
    Ok(())
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

impl Catalog {
    /// Open (or lazily create on first write) a catalog rooted at `root`.
    pub fn open(root: impl AsRef<Path>) -> Result<Self> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root)?;
        let path = root.join("catalog.json");
        let inner = match fs::read(&path) {
            Ok(bytes) => serde_json::from_slice::<CatalogFile>(&bytes).map_err(|e| {
                Error::CatalogCorrupt(format!("catalog.json is not valid JSON: {e}"))
            })?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => CatalogFile {
                version: 1,
                indexes: BTreeMap::new(),
            },
            Err(e) => return Err(e.into()),
        };
        if inner.version != 1 {
            return Err(Error::CatalogCorrupt(format!(
                "unsupported catalog version {}",
                inner.version
            )));
        }
        Ok(Self { root, inner })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn exists(&self, name: &str) -> bool {
        self.inner.indexes.contains_key(name)
    }

    pub fn list(&self) -> Vec<&IndexMeta> {
        self.inner.indexes.values().collect()
    }

    pub fn meta(&self, name: &str) -> Option<&IndexMeta> {
        self.inner.indexes.get(name)
    }

    pub fn index_path(&self, name: &str) -> PathBuf {
        let file = self
            .inner
            .indexes
            .get(name)
            .map(|m| m.file.clone())
            .unwrap_or_else(|| format!("{name}.fm"));
        self.root.join(file)
    }

    /// Build, persist and register a new index.
    pub fn create(&mut self, name: &str, text: Vec<u8>, sample_interval: u32) -> Result<IndexMeta> {
        validate_name(name)?;
        if self.inner.indexes.contains_key(name) {
            return Err(Error::AlreadyExists(name.to_string()));
        }
        let index = FmIndex::build(text, sample_interval)?;
        // Re-ensure the root exists: a freshly opened catalog may have had its
        // directory removed out from under it before the first write.
        fs::create_dir_all(&self.root)?;
        let file = format!("{name}.fm");
        let path = self.root.join(&file);
        write_index_file(&path, name, &index)?;

        let sha = sha256_hex(index.text());
        let meta = IndexMeta {
            name: name.to_string(),
            file,
            text_len: index.text_len(),
            sample_interval,
            sha256: sha,
            created_unix_ms: now_ms(),
        };
        self.inner.indexes.insert(name.to_string(), meta.clone());
        self.flush()?;
        Ok(meta)
    }

    /// Load and fully validate a persisted index.
    pub fn open_index(&self, name: &str) -> Result<FmIndex> {
        let meta = self
            .inner
            .indexes
            .get(name)
            .ok_or_else(|| Error::NotFound(name.to_string()))?;
        let path = self.root.join(&meta.file);
        let (persisted_name, index) = read_index_file(&path)?;
        if persisted_name != name {
            return Err(Error::PersistenceCorrupt {
                section: "header".into(),
                detail: format!("file registered as {name:?} but tagged {persisted_name:?}"),
            });
        }
        if sha256_hex(index.text()) != meta.sha256 {
            return Err(Error::PersistenceCorrupt {
                section: "txt".into(),
                detail: "text SHA-256 does not match catalog record".into(),
            });
        }
        if index.text_len() != meta.text_len || index.sample_interval() != meta.sample_interval {
            return Err(Error::PersistenceCorrupt {
                section: "header".into(),
                detail: "metadata length/sample_interval disagree with file".into(),
            });
        }
        Ok(index)
    }

    /// Remove an index and its catalog record.
    pub fn delete(&mut self, name: &str) -> Result<IndexMeta> {
        let meta = self
            .inner
            .indexes
            .remove(name)
            .ok_or_else(|| Error::NotFound(name.to_string()))?;
        let path = self.root.join(&meta.file);
        match fs::remove_file(path) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
        self.flush()?;
        Ok(meta)
    }

    fn flush(&self) -> Result<()> {
        let path = self.root.join("catalog.json");
        let tmp = self.root.join("catalog.json.tmp");
        let bytes = serde_json::to_vec_pretty(&self.inner).expect("catalog serializes");
        {
            let mut f = fs::File::create(&tmp)?;
            f.write_all(&bytes)?;
            f.sync_all()?;
        }
        fs::rename(&tmp, &path)?;
        Ok(())
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    let digest = h.finalize();
    let mut s = String::with_capacity(64);
    for b in digest {
        use std::fmt::Write;
        let _ = write!(s, "{b:02x}");
    }
    s
}

// ---------------------------------------------------------------------------
// Section primitives (also used directly by corruption tests)
// ---------------------------------------------------------------------------

struct Section {
    tag: [u8; 4],
    payload: Vec<u8>,
}

fn write_section(buf: &mut Vec<u8>, tag: &[u8; 4], payload: &[u8]) {
    buf.extend_from_slice(tag);
    buf.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    let crc = crc32fast::hash(payload);
    buf.extend_from_slice(&crc.to_le_bytes());
    buf.extend_from_slice(payload);
}

fn read_section(rest: &[u8]) -> Result<(Section, &[u8])> {
    if rest.len() < 12 {
        return Err(Error::PersistenceCorrupt {
            section: "container".into(),
            detail: "truncated section header".into(),
        });
    }
    let tag: [u8; 4] = rest[..4].try_into().unwrap();
    let len = u32::from_le_bytes(rest[4..8].try_into().unwrap()) as usize;
    let crc_stored = u32::from_le_bytes(rest[8..12].try_into().unwrap());
    if rest.len() < 12 + len {
        return Err(Error::PersistenceCorrupt {
            section: String::from_utf8_lossy(&tag).into_owned(),
            detail: "declared payload length exceeds file".into(),
        });
    }
    let payload = &rest[12..12 + len];
    if crc32fast::hash(payload) != crc_stored {
        return Err(Error::PersistenceCorrupt {
            section: String::from_utf8_lossy(&tag).into_owned(),
            detail: "CRC-32 mismatch".into(),
        });
    }
    Ok((
        Section {
            tag,
            payload: payload.to_vec(),
        },
        &rest[12 + len..],
    ))
}

fn take_u64<'a>(payload: &'a [u8], section: &str) -> Result<(u64, &'a [u8])> {
    if payload.len() < 8 {
        return Err(Error::PersistenceCorrupt {
            section: section.into(),
            detail: "missing length prefix".into(),
        });
    }
    Ok((
        u64::from_le_bytes(payload[..8].try_into().unwrap()),
        &payload[8..],
    ))
}

fn write_index_file(path: &Path, name: &str, index: &FmIndex) -> Result<()> {
    let mut file = Vec::new();
    file.extend_from_slice(MAGIC);
    file.extend_from_slice(&(name.len() as u32).to_le_bytes());
    file.extend_from_slice(name.as_bytes());
    file.extend_from_slice(&index.sample_interval().to_le_bytes());

    write_section(&mut file, TAG_TXT, index.text());

    let mut bwt_payload = Vec::with_capacity(8 + index.bwt().len() * 2);
    bwt_payload.extend_from_slice(&(index.bwt().len() as u64).to_le_bytes());
    for &s in index.bwt() {
        bwt_payload.extend_from_slice(&s.to_le_bytes());
    }
    write_section(&mut file, TAG_BWT, &bwt_payload);

    let mut sas_payload = Vec::with_capacity(8 + index.sa_samples().len() * 4);
    sas_payload.extend_from_slice(&(index.sa_samples().len() as u64).to_le_bytes());
    for &v in index.sa_samples() {
        sas_payload.extend_from_slice(&v.to_le_bytes());
    }
    write_section(&mut file, TAG_SAS, &sas_payload);

    let tmp = path.with_extension("fm.tmp");
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(&file)?;
        f.sync_all()?;
    }
    fs::rename(&tmp, path)?;
    Ok(())
}

/// Read and parse an index file into a validated [`FmIndex`].
/// Returns `(embedded_name, index)`.
pub fn read_index_file(path: &Path) -> Result<(String, FmIndex)> {
    let bytes = fs::read(path)?;
    parse_index_bytes(&bytes)
}

/// Pure parser (bytes -> index), exposed for corruption tests.
pub fn parse_index_bytes(bytes: &[u8]) -> Result<(String, FmIndex)> {
    let corrupt = |section: &str, detail: String| Error::PersistenceCorrupt {
        section: section.into(),
        detail,
    };
    if bytes.len() < MAGIC.len() + 8 || &bytes[..MAGIC.len()] != MAGIC {
        return Err(corrupt("magic", "bad magic / truncated header".into()));
    }
    let mut pos = MAGIC.len();
    let name_len = u32::from_le_bytes(
        bytes[pos..pos + 4]
            .try_into()
            .map_err(|_| corrupt("header", "short name length".into()))?,
    ) as usize;
    pos += 4;
    if name_len == 0 || name_len > 64 || pos + name_len + 4 > bytes.len() {
        return Err(corrupt("header", "invalid embedded name length".into()));
    }
    let name = String::from_utf8(bytes[pos..pos + name_len].to_vec())
        .map_err(|_| corrupt("header", "embedded name is not UTF-8".into()))?;
    pos += name_len;
    let sample_interval = u32::from_le_bytes(
        bytes[pos..pos + 4]
            .try_into()
            .map_err(|_| corrupt("header", "missing sample interval".into()))?,
    );
    pos += 4;

    let mut rest = &bytes[pos..];
    let mut sections: Vec<Section> = Vec::with_capacity(3);
    for _ in 0..3 {
        let (s, next) = read_section(rest)?;
        sections.push(s);
        rest = next;
    }
    if !rest.is_empty() {
        return Err(corrupt("container", "trailing bytes after sections".into()));
    }
    let expected = [TAG_TXT, TAG_BWT, TAG_SAS];
    for (s, want) in sections.iter().zip(expected) {
        if &s.tag != want {
            return Err(corrupt(
                "container",
                format!(
                    "expected tag {}, found {}",
                    String::from_utf8_lossy(want),
                    String::from_utf8_lossy(&s.tag)
                ),
            ));
        }
    }

    let text = sections[0].payload.clone();

    let (bwt_n, body) = take_u64(&sections[1].payload, "bwt")?;
    if body.len() != bwt_n as usize * 2 {
        return Err(corrupt(
            "bwt",
            "payload length disagrees with symbol count".into(),
        ));
    }
    let (chunks, rest) = body.as_chunks::<2>();
    debug_assert!(rest.is_empty());
    let bwt: Vec<u16> = chunks.iter().map(|c| u16::from_le_bytes(*c)).collect();

    let (sas_n, body) = take_u64(&sections[2].payload, "sa_samples")?;
    if body.len() != sas_n as usize * 4 {
        return Err(corrupt(
            "sa_samples",
            "payload length disagrees with sample count".into(),
        ));
    }
    let (chunks, rest) = body.as_chunks::<4>();
    debug_assert!(rest.is_empty());
    let sa_samples: Vec<u32> = chunks.iter().map(|c| u32::from_le_bytes(*c)).collect();

    // The sample interval is explicit in the header; the sample count must
    // agree with it (cross-checked again inside FmIndex::from_persisted).
    let index = FmIndex::from_persisted(text, bwt, sample_interval, sa_samples)?;
    Ok((name, index))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn name_rules() {
        assert!(validate_name("ok-1_2").is_ok());
        assert!(validate_name("").is_err());
        assert!(validate_name("../escape").is_err());
        assert!(validate_name("has space").is_err());
        assert!(validate_name(&"a".repeat(65)).is_err());
    }

    #[test]
    fn round_trip_and_reload() {
        let dir = tempfile::tempdir().unwrap();
        let mut cat = Catalog::open(dir.path()).unwrap();
        let meta = cat.create("demo", b"abracadabra".to_vec(), 3).unwrap();
        assert_eq!(meta.text_len, 11);

        let reopened = Catalog::open(dir.path()).unwrap();
        let idx = reopened.open_index("demo").unwrap();
        let out = idx.search(b"abr");
        assert_eq!(out.positions, vec![0, 7]);
        assert!(reopened.exists("demo"));

        reopened.meta("demo").unwrap();
        let mut cat2 = Catalog::open(dir.path()).unwrap();
        assert!(cat2.delete("demo").is_ok());
        assert!(!cat2.exists("demo"));
        assert!(matches!(cat2.open_index("demo"), Err(Error::NotFound(_))));
    }
}
