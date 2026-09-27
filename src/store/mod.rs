//! Filesystem persistence adapter.
//!
//! Layout under the configured data directory:
//! ```text
//! <dir>/events.jsonl   append-only committed event log (one JSON object/line)
//! <dir>/CURRENT        single decimal number: latest published version id
//! <dir>/CURRENT.tmp    staging file for atomic CURRENT replacement
//! ```
//! Every log line carries an FNV-1a-64 checksum over a canonical (sorted-key)
//! JSON rendering of its fields, so external truncation, bit flips or manual
//! edits are detected on replay instead of being silently accepted. Each
//! commit is `fsync`ed; `CURRENT` is replaced via rename + directory fsync.
//!
//! The service applies events **before** they are committed and only publishes
//! them to readers **after** the commit returns — so a reader never observes a
//! version whose durability was not confirmed, and replay cannot skip it.

use crate::model::PointUpdate;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};

const LOG_NAME: &str = "events.jsonl";
const CURRENT_NAME: &str = "CURRENT";
const CURRENT_TMP: &str = "CURRENT.tmp";

/// Error raised by the persistence layer; the service maps it onto
/// [`crate::errors::AppError::Persistence`].
#[derive(Debug)]
pub enum StoreError {
    Io(String, std::io::Error),
    /// (line number 1-based, explanation)
    Corrupt(usize, String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(path, e) => write!(f, "I/O error on {path}: {e}"),
            StoreError::Corrupt(line, why) => {
                write!(f, "corrupt event log at line {line}: {why}")
            }
        }
    }
}

impl std::error::Error for StoreError {}

fn io_err(path: &Path, e: std::io::Error) -> StoreError {
    StoreError::Io(path.display().to_string(), e)
}

/// One committed domain event. Version ids are dense: record `i` publishes
/// version `i`; `seq` is the log line number and equals the version id.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum EventPayload {
    /// Initial coordinate registration (exactly one per log, first event).
    Registered { xs: Vec<i64>, ys: Vec<i64> },
    /// Atomic point-increment batch.
    Batch { updates: Vec<PointUpdate> },
    /// Coordinate-table rebuild (carry-over of surviving cell totals is
    /// recomputed deterministically by the service on replay).
    Rebuilt { xs: Vec<i64>, ys: Vec<i64> },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventRecord {
    pub seq: u64,
    pub version: u64,
    pub payload: EventPayload,
}

/// FNV-1a 64-bit, rendered as 16 lowercase hex digits.
fn fnv1a64_hex(bytes: &[u8]) -> String {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut hash = OFFSET;
    for &b in bytes {
        hash ^= b as u64;
        hash = hash.wrapping_mul(PRIME);
    }
    format!("{hash:016x}")
}

/// Parse a record, verify its checksum, and return it.
///
/// Verification re-renders the record (without `hash`) through a `BTreeMap`,
/// so the checksum is independent of on-disk key ordering.
fn verify_line(line_no: usize, raw: &str) -> Result<EventRecord, StoreError> {
    let mut obj: BTreeMap<String, serde_json::Value> = serde_json::from_str(raw)
        .map_err(|e| StoreError::Corrupt(line_no, format!("invalid JSON: {e}")))?;

    let want = obj
        .remove("hash")
        .and_then(|v| v.as_str().map(str::to_owned))
        .ok_or_else(|| StoreError::Corrupt(line_no, "missing 'hash' field".to_string()))?;

    let canonical = serde_json::to_string(&obj)
        .map_err(|e| StoreError::Corrupt(line_no, format!("cannot re-canonicalize: {e}")))?;
    let got = fnv1a64_hex(canonical.as_bytes());
    if got != want {
        return Err(StoreError::Corrupt(
            line_no,
            format!("checksum mismatch (line claims {want}, canonical gives {got})"),
        ));
    }

    #[derive(Deserialize)]
    struct Core {
        seq: u64,
        version: u64,
        event: EventPayload,
    }
    let core: Core = serde_json::from_str(raw)
        .map_err(|e| StoreError::Corrupt(line_no, format!("missing/malformed core fields: {e}")))?;
    Ok(EventRecord {
        seq: core.seq,
        version: core.version,
        payload: core.event,
    })
}

/// All data recovered from an opened store.
#[derive(Debug)]
pub struct Replay {
    pub records: Vec<EventRecord>,
    /// `None` when the log is empty (fresh directory).
    pub current: Option<u64>,
}

pub struct FsStore {
    dir: PathBuf,
    writer: BufWriter<File>,
    next_seq: u64,
}

impl FsStore {
    /// Open (creating if needed) the data directory and validate its contents.
    /// A truncated/edited log or a stale `CURRENT` is a hard error: the
    /// service refuses to start over unknown state.
    pub fn open(dir: impl AsRef<Path>) -> Result<(Self, Replay), StoreError> {
        let dir = dir.as_ref().to_path_buf();
        fs::create_dir_all(&dir).map_err(|e| io_err(&dir, e))?;

        let log_path = dir.join(LOG_NAME);
        let current_path = dir.join(CURRENT_NAME);

        let mut records = Vec::new();
        if log_path.exists() {
            let file = File::open(&log_path).map_err(|e| io_err(&log_path, e))?;
            for (idx, line) in BufReader::new(file).lines().enumerate() {
                let line_no = idx + 1;
                let line = line.map_err(|e| io_err(&log_path, e))?;
                if line.trim().is_empty() {
                    return Err(StoreError::Corrupt(
                        line_no,
                        "blank line inside log".to_string(),
                    ));
                }
                let rec = verify_line(line_no, &line)?;
                if rec.seq as usize != line_no {
                    return Err(StoreError::Corrupt(
                        line_no,
                        format!("seq {} out of order (expected {line_no})", rec.seq),
                    ));
                }
                if rec.version != rec.seq {
                    return Err(StoreError::Corrupt(
                        line_no,
                        format!("version {} does not match seq {}", rec.version, rec.seq),
                    ));
                }
                records.push(rec);
            }
        }

        let current = if current_path.exists() {
            let text = fs::read_to_string(&current_path).map_err(|e| io_err(&current_path, e))?;
            let parsed: u64 = text.trim().parse().map_err(|_| {
                StoreError::Io(
                    current_path.display().to_string(),
                    std::io::Error::new(std::io::ErrorKind::InvalidData, "CURRENT is not a u64"),
                )
            })?;
            Some(parsed)
        } else {
            None
        };

        match (current, records.last().map(|r| r.version)) {
            (Some(c), Some(last)) if c == last => {}
            (None, None) => {}
            (Some(c), Some(last)) => {
                return Err(StoreError::Io(
                    current_path.display().to_string(),
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        format!("CURRENT={c} disagrees with log tail version {last}"),
                    ),
                ));
            }
            (Some(c), None) => {
                return Err(StoreError::Io(
                    current_path.display().to_string(),
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        format!("CURRENT={c} but event log is empty"),
                    ),
                ));
            }
            (None, Some(last)) => {
                return Err(StoreError::Corrupt(
                    records.len(),
                    format!("log reaches version {last} but CURRENT is missing"),
                ));
            }
        }

        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|e| io_err(&log_path, e))?;
        let next_seq = records.len() as u64 + 1;

        Ok((
            Self {
                dir,
                writer: BufWriter::new(file),
                next_seq,
            },
            Replay { records, current },
        ))
    }

    /// Durably append one event and advance `CURRENT`. Both are synced before
    /// the call returns; on error the process should be considered crashed and
    /// replay reconciles state.
    pub fn commit(&mut self, payload: EventPayload) -> Result<EventRecord, StoreError> {
        let seq = self.next_seq;
        let record = EventRecord {
            seq,
            version: seq,
            payload,
        };

        // Canonical core: BTreeMap gives a fixed key order independent of the
        // serde field order used elsewhere.
        let mut core: BTreeMap<String, serde_json::Value> = BTreeMap::new();
        core.insert("seq".into(), serde_json::json!(record.seq));
        core.insert("version".into(), serde_json::json!(record.version));
        core.insert(
            "event".into(),
            serde_json::to_value(&record.payload).expect("payload always serializes"),
        );
        let canonical = serde_json::to_string(&core).expect("canonical serialization cannot fail");
        let hash = fnv1a64_hex(canonical.as_bytes());

        let mut line = BTreeMap::new();
        line.extend(core);
        line.insert("hash".into(), serde_json::json!(hash));
        let rendered = serde_json::to_string(&line).expect("line serialization cannot fail");

        self.writer
            .write_all(rendered.as_bytes())
            .and_then(|_| self.writer.write_all(b"\n"))
            .and_then(|_| self.writer.flush())
            .map_err(|e| io_err(&self.dir.join(LOG_NAME), e))?;
        self.writer
            .get_ref()
            .sync_all()
            .map_err(|e| io_err(&self.dir.join(LOG_NAME), e))?;

        self.write_current(seq)?;
        self.next_seq += 1;
        Ok(record)
    }

    fn write_current(&mut self, version: u64) -> Result<(), StoreError> {
        let tmp = self.dir.join(CURRENT_TMP);
        let cur = self.dir.join(CURRENT_NAME);
        let mut f = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&tmp)
            .map_err(|e| io_err(&tmp, e))?;
        f.write_all(format!("{version}\n").as_bytes())
            .and_then(|_| f.sync_all())
            .map_err(|e| io_err(&tmp, e))?;
        drop(f);
        fs::rename(&tmp, &cur).map_err(|e| io_err(&cur, e))?;
        Self::sync_dir(&self.dir)?;
        Ok(())
    }

    /// fsync the directory so the rename of CURRENT is durable. On Linux
    /// `fsync(2)` is defined on directory file descriptors; std's safe
    /// `File::sync_all` issues exactly that syscall, so no `unsafe` is needed.
    fn sync_dir(dir: &Path) -> Result<(), StoreError> {
        #[cfg(unix)]
        {
            let f = File::open(dir).map_err(|e| io_err(dir, e))?;
            f.sync_all().map_err(|e| io_err(dir, e))?;
        }
        #[cfg(not(unix))]
        {
            let _ = dir;
        }
        Ok(())
    }
}
