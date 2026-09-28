//! 文件系统持久化适配：仅追加（append-only）WAL。
//!
//! 帧格式（小端）：
//! ```text
//! magic(8) | seq(u64) | payload_len(u32) | payload(payload_len, JSON) | crc32(u32)
//! ```
//! CRC32 覆盖 `seq || payload_len || payload`，用于检测截断与位翻转。
//! 读取时魔数/长度/CRC/JSON/语义序号任一不符都报 `CORRUPT_LOG`，
//! 不会把损坏日志静默当成空状态启动（未知状态绝不报告成功）。
//!
//! 发布顺序：先构造并校验新快照（内核），再追加 WAL（`write_all + flush`），
//! 最后在锁内原子替换内存快照。因此查询永远看不到“半批”。

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::model::PointUpdate;

const MAGIC: &[u8; 8] = b"PR2DWAL1";
const HEADER_LEN: usize = 8 + 8 + 4;

/// 建表事件。坐标顺序就是输入顺序（内核负责排序去重）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TableRegistered {
    pub table_id: u32,
    pub xs: Vec<i64>,
    pub ys: Vec<i64>,
    pub created_at_ms: i64,
}

/// 批发布事件。存原始增量条目与压缩后格点，回放时无需重新判注册。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StoredPoint {
    pub ix: usize,
    pub iy: usize,
    pub delta: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BatchCommitted {
    pub table_id: u32,
    /// 表内新版本号（从 1 开始）。
    pub version: u64,
    pub base_version: u64,
    pub updates: Vec<PointUpdate>,
    pub cells: Vec<StoredPoint>,
    pub created_at_ms: i64,
}

/// WAL 事件联合。`tag` 写在 JSON 内（1=注册，2=批发布）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    TableRegistered(TableRegistered),
    BatchCommitted(BatchCommitted),
}

#[derive(Serialize, Deserialize)]
#[serde(tag = "tag")]
enum EventWire {
    #[serde(rename = "1")]
    T1(TableRegistered),
    #[serde(rename = "2")]
    T2(BatchCommitted),
}

impl Event {
    fn to_wire(&self) -> EventWire {
        match self {
            Event::TableRegistered(e) => EventWire::T1(e.clone()),
            Event::BatchCommitted(e) => EventWire::T2(e.clone()),
        }
    }

    fn from_wire(w: EventWire) -> Event {
        match w {
            EventWire::T1(e) => Event::TableRegistered(e),
            EventWire::T2(e) => Event::BatchCommitted(e),
        }
    }
}

// ---------- CRC32 (IEEE 802.3, 与 zlib 同一多项式) ----------

struct Crc32 {
    table: [u32; 256],
}

impl Crc32 {
    fn new() -> Crc32 {
        let mut table = [0u32; 256];
        for i in 0..256u32 {
            let mut c = i;
            for _ in 0..8 {
                c = if c & 1 != 0 {
                    0xEDB8_8320 ^ (c >> 1)
                } else {
                    c >> 1
                };
            }
            table[i as usize] = c;
        }
        Crc32 { table }
    }

    fn checksum(&self, data: &[u8]) -> u32 {
        let mut crc = 0xFFFF_FFFFu32;
        for &b in data {
            crc = self.table[((crc ^ b as u32) & 0xFF) as usize] ^ (crc >> 8);
        }
        crc ^ 0xFFFF_FFFF
    }
}

// ---------- EventLog ----------

pub struct EventLog {
    path: PathBuf,
    file: File,
    crc: Crc32,
    /// 下一条事件的全局物理序号（从 1 开始）。
    next_seq: u64,
    /// 下一条记录的字节起始偏移（损坏报告用）。
    next_offset: u64,
}

impl EventLog {
    /// 打开（必要时创建）WAL。`fresh=true` 时新建空文件并 fsync 目录；
    /// `fresh=false` 时只做读校验（调用方随后 [`EventLog::read_all`]）。
    pub fn open(dir: &Path) -> CoreResult<EventLog> {
        std::fs::create_dir_all(dir)
            .map_err(|e| CoreError::Storage(format!("create data dir: {e}")))?;
        let path = dir.join("wal.log");
        let existed = path.exists();
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .read(true)
            .open(&path)
            .map_err(|e| CoreError::Storage(format!("open wal {}: {e}", path.display())))?;
        if !existed {
            // 保证新建文件与其目录项在崩溃后仍可见。
            file.sync_all()
                .map_err(|e| CoreError::Storage(format!("fsync new wal: {e}")))?;
            fsync_dir(dir)?;
        }
        Ok(EventLog {
            path,
            file,
            crc: Crc32::new(),
            next_seq: 1,
            next_offset: 0,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// 读出并校验全部事件，同时推进内部序号/偏移。
    pub fn read_all(&mut self) -> CoreResult<Vec<(u64, Event)>> {
        let mut data = Vec::new();
        self.file
            .read_to_end(&mut data)
            .map_err(|e| CoreError::Storage(format!("read wal: {e}")))?;
        let mut out = Vec::new();
        let mut pos = 0usize;
        while pos < data.len() {
            let record_start = pos as u64;
            if data.len() - pos < HEADER_LEN {
                return Err(CoreError::CorruptLog {
                    offset: record_start,
                    detail: format!("truncated header: {} bytes remain", data.len() - pos),
                });
            }
            if &data[pos..pos + 8] != MAGIC {
                return Err(CoreError::CorruptLog {
                    offset: record_start,
                    detail: "bad magic".into(),
                });
            }
            let seq = u64::from_le_bytes(data[pos + 8..pos + 16].try_into().unwrap());
            let payload_len =
                u32::from_le_bytes(data[pos + 16..pos + 20].try_into().unwrap()) as usize;
            let total = HEADER_LEN + payload_len + 4;
            if data.len() - pos < total {
                return Err(CoreError::CorruptLog {
                    offset: record_start,
                    detail: format!(
                        "truncated payload: need {total} bytes, {} remain",
                        data.len() - pos
                    ),
                });
            }
            let payload = &data[pos + HEADER_LEN..pos + HEADER_LEN + payload_len];
            let stored_crc = u32::from_le_bytes(
                data[pos + HEADER_LEN + payload_len..pos + total]
                    .try_into()
                    .unwrap(),
            );

            // CRC 覆盖 seq + len + payload。
            let mut covered = Vec::with_capacity(8 + 4 + payload_len);
            covered.extend_from_slice(&seq.to_le_bytes());
            covered.extend_from_slice(&(payload_len as u32).to_le_bytes());
            covered.extend_from_slice(payload);
            let actual_crc = self.crc.checksum(&covered);
            if actual_crc != stored_crc {
                return Err(CoreError::CorruptLog {
                    offset: record_start,
                    detail: format!(
                        "crc mismatch: stored={stored_crc:#010x}, actual={actual_crc:#010x}"
                    ),
                });
            }
            if seq != out.len() as u64 + 1 {
                return Err(CoreError::CorruptLog {
                    offset: record_start,
                    detail: format!(
                        "non-contiguous seq: expected {}, found {seq}",
                        out.len() + 1
                    ),
                });
            }
            let wire: EventWire =
                serde_json::from_slice(payload).map_err(|e| CoreError::CorruptLog {
                    offset: record_start,
                    detail: format!("bad payload json: {e}"),
                })?;
            out.push((seq, Event::from_wire(wire)));
            pos += total;
        }
        self.next_seq = out.len() as u64 + 1;
        self.next_offset = pos as u64;
        Ok(out)
    }

    /// 追加一条事件（fsync 后返回）。返回 (全局序号, 起始字节偏移)。
    pub fn append(&mut self, ev: &Event) -> CoreResult<(u64, u64)> {
        let payload = serde_json::to_vec(&ev.to_wire())
            .map_err(|e| CoreError::Storage(format!("encode event: {e}")))?;
        let seq = self.next_seq;
        let offset = self.next_offset;
        let payload_len = payload.len() as u32;

        let mut covered = Vec::with_capacity(8 + 4 + payload.len());
        covered.extend_from_slice(&seq.to_le_bytes());
        covered.extend_from_slice(&payload_len.to_le_bytes());
        covered.extend_from_slice(&payload);
        let crc = self.crc.checksum(&covered);

        let mut frame = Vec::with_capacity(HEADER_LEN + payload.len() + 4);
        frame.extend_from_slice(MAGIC);
        frame.extend_from_slice(&covered);
        frame.extend_from_slice(&crc.to_le_bytes());

        // 单次 write_all + flush：崩溃只会留下“有”或“没有”整条记录
        // （部分写由 CRC/长度校验在重放时识别为 CORRUPT_LOG，绝不半应用）。
        self.file
            .write_all(&frame)
            .map_err(|e| CoreError::Storage(format!("append wal: {e}")))?;
        self.file
            .flush()
            .map_err(|e| CoreError::Storage(format!("flush wal: {e}")))?;
        self.file
            .sync_all()
            .map_err(|e| CoreError::Storage(format!("fsync wal: {e}")))?;

        self.next_seq += 1;
        self.next_offset += frame.len() as u64;
        Ok((seq, offset))
    }
}

fn fsync_dir(dir: &Path) -> CoreResult<()> {
    let f = File::open(dir).map_err(|e| CoreError::Storage(format!("open dir for fsync: {e}")))?;
    f.sync_all()
        .map_err(|e| CoreError::Storage(format!("fsync dir: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "pr2d-wal-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    fn sample_events() -> Vec<Event> {
        vec![
            Event::TableRegistered(TableRegistered {
                table_id: 1,
                xs: vec![1, 2, 2],
                ys: vec![9],
                created_at_ms: 1,
            }),
            Event::BatchCommitted(BatchCommitted {
                table_id: 1,
                version: 1,
                base_version: 0,
                updates: vec![PointUpdate {
                    x: 1,
                    y: 9,
                    delta: -5,
                }],
                cells: vec![StoredPoint {
                    ix: 0,
                    iy: 0,
                    delta: -5,
                }],
                created_at_ms: 2,
            }),
        ]
    }

    #[test]
    fn append_and_replay_roundtrip() {
        let dir = tmp("rt");
        let mut log = EventLog::open(&dir).unwrap();
        for ev in sample_events() {
            log.append(&ev).unwrap();
        }
        drop(log);
        let mut log2 = EventLog::open(&dir).unwrap();
        let got = log2.read_all().unwrap();
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].1, sample_events()[0]);
        assert_eq!(got[1].1, sample_events()[1]);
        // 重放后继续追加，序号必须连续。
        let (seq, _) = log2.append(&sample_events()[0]).unwrap();
        assert_eq!(seq, 3);
    }

    #[test]
    fn detects_truncation_and_bitflip() {
        let dir = tmp("bad");
        {
            let mut log = EventLog::open(&dir).unwrap();
            for ev in sample_events() {
                log.append(&ev).unwrap();
            }
        }
        let path = dir.join("wal.log");
        let raw = std::fs::read(&path).unwrap();
        // 截断最后 3 个字节
        std::fs::write(&path, &raw[..raw.len() - 3]).unwrap();
        let mut log = EventLog::open(&dir).unwrap();
        let err = log.read_all().unwrap_err();
        assert_eq!(err.code(), "CORRUPT_LOG");
        // 翻转第一条记录 payload 中的一个比特
        std::fs::write(&path, &raw).unwrap();
        let mut flipped = raw.clone();
        flipped[HEADER_LEN + 2] ^= 0x01;
        std::fs::write(&path, &flipped).unwrap();
        let mut log = EventLog::open(&dir).unwrap();
        assert_eq!(log.read_all().unwrap_err().code(), "CORRUPT_LOG");
    }
}
