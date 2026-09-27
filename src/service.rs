//! 服务状态机：把过滤器、令牌、记账、持久化组合为可执行的插入/查询/删除事务。
//!
//! 所有修改在单个互斥区内完成并**写透持久化**：只有快照原子落盘成功，一次
//! 插入/删除才对外返回成功；落盘失败则内存改动随锁内回滚一并撤销（见
//! [`Service::insert`] / [`Service::delete`]）。因此磁盘与内存不会停留在
//! 「删除了但没存上」的半状态。

use std::path::PathBuf;
use std::sync::Mutex;

use serde::Serialize;

use crate::credentials::CredentialError;
use crate::filter::{CuckooFilter, Deletion, InsertOutcome};
use crate::hashing::{place, KernelParams};
use crate::ledger::Ledger;
use crate::persistence::{
    decode_snapshot, encode_snapshot, load_or_create_signing_secret, now_ms, write_snapshot_atomic,
    Counters, Loaded, PersistError,
};
use crate::token::{self, SigningKey};

/// 服务层错误，携带具体失败类别。
#[derive(Debug)]
pub enum ServiceError {
    /// 过滤器容量耗尽（插入回滚完成）。
    FilterFull,
    /// 单键副本数达到上限。
    DuplicateLimit,
    /// 凭证问题（伪造/重放/未知键/越界序号）。
    Credential(CredentialError),
    /// 过滤器内部不变式被破坏（如记账有记录但桶中无指纹）。
    Invariant(String),
    /// 持久化失败（IO）；内存事务已回滚。
    Persist(String),
    /// 启动期快照损坏。
    SnapshotCorrupt(String),
    /// 键为空或过长。
    BadKey(String),
}

impl std::fmt::Display for ServiceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ServiceError::FilterFull => f.write_str("过滤器容量耗尽，插入已回滚"),
            ServiceError::DuplicateLimit => f.write_str("该键副本数已达上限"),
            ServiceError::Credential(c) => write!(f, "删除凭证无效: {c}"),
            ServiceError::Invariant(s) => write!(f, "内部不变式破坏: {s}"),
            ServiceError::Persist(s) => write!(f, "持久化失败（事务已回滚）: {s}"),
            ServiceError::SnapshotCorrupt(s) => write!(f, "快照无法加载: {s}"),
            ServiceError::BadKey(s) => write!(f, "键非法: {s}"),
        }
    }
}
impl std::error::Error for ServiceError {}

/// 插入结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InsertReceipt {
    /// true 表示这是该键的第一张凭证（新占用一个槽）；false 表示重复插入。
    pub newly_occupied: bool,
    /// 该键当前存活副本数。
    pub live_count: u64,
    /// 本次签发的删除凭证。
    pub token: String,
    /// 本次迁移次数（重复插入为 0）。
    pub kicks: u32,
}

/// 删除结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeleteReceipt {
    pub key_id_b64: String,
    pub ordinal: u64,
    pub live_count: u64,
}

/// 成员查询结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Membership {
    /// 过滤器判定。可能假阳性；对存活键永不为 false。
    pub member: bool,
    /// 记账层的精确存活副本数（不对外暴露敏感细节时由 API 决定是否返回）。
    pub exact_live: u64,
}

/// 统计信息。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Stats {
    pub version: String,
    pub kernel_version: u32,
    pub snapshot_format_version: u32,
    pub token_version: u8,
    pub run_id: String,
    pub num_buckets: u64,
    pub bucket_size: u32,
    pub fingerprint_bits: u32,
    pub max_kicks: u32,
    pub occupied_slots: u64,
    pub total_slots: u64,
    pub load_factor: f64,
    pub live_keys: usize,
    pub live_copies: u64,
    pub counters: Counters,
}

#[derive(Debug)]
struct Inner {
    params: KernelParams,
    filter: CuckooFilter,
    ledger: Ledger,
    counters: Counters,
}

/// 过滤器服务（线程安全）。
#[derive(Debug)]
pub struct Service {
    inner: Mutex<Inner>,
    signing: SigningKey,
    snapshot_path: PathBuf,
    fsync: bool,
    run_id: String,
    version: String,
    kernel_version: u32,
    snapshot_format_version: u32,
}

impl Service {
    /// 启动：准备密钥、加载（或初始化）快照。
    ///
    /// 参数较多是刻意为之：启动引导需要显式的内核参数、路径、密钥与全部版本/身份信息，
    /// 打包成结构体反而会隐藏这些必填依赖，故在此允许 clippy 的参数数量告警。
    #[allow(clippy::too_many_arguments)]
    pub fn open(
        params: KernelParams,
        data_dir: &std::path::Path,
        snapshot_name: &str,
        configured_secret_hex: &str,
        fsync: bool,
        run_id: String,
        version: String,
        kernel_version: u32,
        snapshot_format_version: u32,
    ) -> Result<Self, ServiceError> {
        std::fs::create_dir_all(data_dir).map_err(|e| ServiceError::Persist(e.to_string()))?;
        let secret_path = data_dir.join("secret.key");
        let secret = load_or_create_signing_secret(configured_secret_hex, &secret_path)
            .map_err(|e| ServiceError::Persist(format!("HMAC 密钥不可用: {e}")))?;
        let signing = SigningKey::from_bytes(secret);
        let snapshot_path = data_dir.join(snapshot_name);

        let inner = match std::fs::read(&snapshot_path) {
            Ok(bytes) => {
                let Loaded {
                    params: p,
                    filter,
                    ledger,
                    counters,
                } = decode_snapshot(&bytes, &params).map_err(|e| match e {
                    PersistError::Corrupt(s) => ServiceError::SnapshotCorrupt(s),
                    PersistError::Io(s) => ServiceError::SnapshotCorrupt(s),
                })?;
                tracing::info!(run_id = %run_id, occupied = filter.occupied_slots(),
                    live = ledger.total_live(), "已加载现有快照");
                Inner {
                    params: p,
                    filter,
                    ledger,
                    counters,
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                let ts = now_ms();
                let inner = Inner {
                    filter: CuckooFilter::new(params.clone()),
                    ledger: Ledger::new(),
                    counters: Counters {
                        created_unix_ms: ts,
                        updated_unix_ms: ts,
                        ..Default::default()
                    },
                    params,
                };
                tracing::info!(run_id = %run_id, "未发现快照，初始化空过滤器");
                inner
            }
            Err(e) => return Err(ServiceError::Persist(format!("读取快照: {e}"))),
        };

        Ok(Self {
            inner: Mutex::new(inner),
            signing,
            snapshot_path,
            fsync,
            run_id,
            version,
            kernel_version,
            snapshot_format_version,
        })
    }

    fn validate_key(&self, key: &[u8]) -> Result<(), ServiceError> {
        if key.is_empty() {
            return Err(ServiceError::BadKey("键不能为空".into()));
        }
        if key.len() > (1 << 20) {
            return Err(ServiceError::BadKey(format!("键过长：{} 字节", key.len())));
        }
        Ok(())
    }

    /// 插入键并签发删除凭证。
    pub fn insert(&self, key: &[u8], rng_seed: Option<u64>) -> Result<InsertReceipt, ServiceError> {
        self.validate_key(key)?;
        let mut g = self.inner.lock().expect("service mutex poisoned");

        let id = token::key_id(key);
        let outcome = match rng_seed {
            Some(seed) => g.filter.insert_seeded(key, id, seed),
            None => g.filter.insert(key, id),
        };
        let pl = place(key, &g.params);

        match outcome {
            InsertOutcome::FilterFull { attempted_kicks } => {
                // 过滤器已自行回滚；不记账、不持久化。
                g.counters.insert_full += 1;
                tracing::warn!(
                    run_id = %self.run_id, key_id = %b64(&id), attempted_kicks,
                    occupied_cells = g.filter.occupied_slots(),
                    "插入失败：容量耗尽，迁移回滚完成"
                );
                return Err(ServiceError::FilterFull);
            }
            InsertOutcome::DuplicateLimit => {
                return Err(ServiceError::DuplicateLimit);
            }
            InsertOutcome::Duplicate => {
                g.counters.insert_dup += 1;
            }
            InsertOutcome::Inserted { kicks } => {
                g.counters.insert_ok += 1;
                tracing::debug!(
                    run_id = %self.run_id, key_id = %b64(&id), kicks,
                    i1 = pl.i1, i2 = pl.i2, fp = pl.fingerprint,
                    "插入完成"
                );
            }
        }

        let kicks = match outcome {
            InsertOutcome::Inserted { kicks } => kicks,
            _ => 0,
        };
        let newly_occupied = matches!(outcome, InsertOutcome::Inserted { .. });

        // 记账（重复键也签发新的、唯一序号的令牌）。
        let ordinal = g.ledger.record_issue(id);
        let tok = token::issue(&self.signing, id, ordinal, pl.i1, pl.i2, pl.fingerprint);

        // 持久化；失败则撤销内存事务。
        g.counters.updated_unix_ms = now_ms();
        if let Err(e) = self.persist_locked(&g) {
            undo_insert(&mut g, id, ordinal, newly_occupied, key);
            return Err(ServiceError::Persist(e));
        }

        Ok(InsertReceipt {
            newly_occupied,
            live_count: g.ledger.live_count(id),
            token: tok,
            kicks,
        })
    }

    /// 成员查询（只读，永不改变状态）。
    pub fn contains(&self, key: &[u8]) -> bool {
        let g = self.inner.lock().expect("service mutex poisoned");
        g.filter.contains(key)
    }

    /// 带精确计数的查询（统计/调试使用）。
    pub fn membership(&self, key: &[u8]) -> Membership {
        let g = self.inner.lock().expect("service mutex poisoned");
        let id = token::key_id(key);
        Membership {
            member: g.filter.contains(key),
            exact_live: g.ledger.live_count(id),
        }
    }

    /// 持凭证删除。令牌校验、记账、桶删除、持久化构成一个事务。
    pub fn delete(&self, token_str: &str) -> Result<DeleteReceipt, ServiceError> {
        let cap = match token::verify(token_str, &self.signing) {
            Ok(c) => c,
            Err(e) => {
                // 签名/编码级拒绝也要计数并落盘（尽力而为），便于审计暴力探测。
                self.note_denial(None, None, &e);
                return Err(ServiceError::Credential(e));
            }
        };

        let mut g = self.inner.lock().expect("service mutex poisoned");

        // 1) 精确授权检查：键存在、序号已签发、未重放。
        if let Err(e) = g.ledger.check_spend(cap.key_id, cap.ordinal) {
            self.note_denial_locked(&mut g, Some(cap.key_id), Some(cap.ordinal), &e);
            return Err(ServiceError::Credential(e));
        }

        // 2) 先标记花费再动桶表，确保即使 decrement 出错也不会留下「可重放令牌」。
        g.ledger.mark_spent(cap.key_id, cap.ordinal);

        // 3) 桶表按 (指纹, owner) 精确递减——其他键的槽位不可能被触碰。
        //    定位（i1/i2/指纹）来自被签名的插入凭证，与 place(key) 一致。
        let deletion: Deletion = g
            .filter
            .decrement(cap.key_id, cap.i1, cap.i2, cap.fingerprint)
            .map_err(|e| {
                g.ledger.unmark_spent(cap.key_id, cap.ordinal);
                ServiceError::Invariant(e.to_string())
            })?;

        g.counters.delete_ok += 1;
        tracing::info!(
            run_id = %self.run_id, key_id = %b64(&cap.key_id), ordinal = cap.ordinal,
            bucket = deletion.bucket, slot = deletion.slot,
            copies_before = deletion.copies_before,
            "持凭证删除成功"
        );

        // 4) 持久化；失败整体撤销。
        g.counters.updated_unix_ms = now_ms();
        if let Err(e) = self.persist_locked(&g) {
            g.filter.restore_deletion(&deletion);
            g.ledger.unmark_spent(cap.key_id, cap.ordinal);
            g.counters.delete_ok = g.counters.delete_ok.saturating_sub(1);
            return Err(ServiceError::Persist(e));
        }

        Ok(DeleteReceipt {
            key_id_b64: b64(&cap.key_id),
            ordinal: cap.ordinal,
            live_count: g.ledger.live_count(cap.key_id),
        })
    }

    /// 快照统计。
    pub fn stats(&self) -> Stats {
        let g = self.inner.lock().expect("service mutex poisoned");
        let total = g.filter.total_slots();
        Stats {
            version: self.version.clone(),
            kernel_version: self.kernel_version,
            snapshot_format_version: self.snapshot_format_version,
            token_version: crate::persistence::token_version(),
            run_id: self.run_id.clone(),
            num_buckets: g.filter.num_buckets(),
            bucket_size: g.filter.bucket_size(),
            fingerprint_bits: g.filter.fingerprint_bits(),
            max_kicks: g.params.max_kicks,
            occupied_slots: g.filter.occupied_slots(),
            total_slots: total,
            load_factor: if total == 0 {
                0.0
            } else {
                g.filter.occupied_slots() as f64 / total as f64
            },
            live_keys: g.ledger.live_keys(),
            live_copies: g.ledger.total_live(),
            counters: g.counters.clone(),
        }
    }

    /// 仅测试/关机时使用：强制再落盘一次。
    pub fn force_flush(&self) -> Result<(), String> {
        let g = self.inner.lock().expect("service mutex poisoned");
        self.persist_locked(&g)
    }

    /// 锁外记录一次凭证拒绝（签名/编码错误，拿不到可信 key_id）。
    fn note_denial(&self, id: Option<[u8; 32]>, ordinal: Option<u64>, e: &CredentialError) {
        match self.inner.lock() {
            Ok(mut g) => self.note_denial_locked(&mut g, id, ordinal, e),
            Err(_) => tracing::error!("互斥锁中毒，拒绝计数未落盘"),
        }
    }

    fn note_denial_locked(
        &self,
        g: &mut Inner,
        id: Option<[u8; 32]>,
        ordinal: Option<u64>,
        e: &CredentialError,
    ) {
        g.counters.delete_denied += 1;
        tracing::warn!(
            run_id = %self.run_id,
            key_id = id.as_ref().map(b64).unwrap_or_else(|| "<unverifiable>".into()),
            ordinal = ordinal.map(|o| o.to_string()).unwrap_or_else(|| "?".into()),
            error = %e, "删除被拒绝：凭证无效"
        );
        g.counters.updated_unix_ms = now_ms();
        // 拒绝不改变可删除状态；仅持久化审计计数（失败只记日志，不影响判定结果）。
        if let Err(perr) = self.persist_locked(g) {
            tracing::error!(error = %perr, "拒绝计数落盘失败");
        }
    }

    fn persist_locked(&self, g: &Inner) -> Result<(), String> {
        let bytes = encode_snapshot(&g.params, &g.filter, &g.ledger, &g.counters)
            .map_err(|e| e.to_string())?;
        write_snapshot_atomic(&self.snapshot_path, &bytes, self.fsync).map_err(|e| e.to_string())
    }
}

/// 插入持久化失败后的内存撤销（与成功插入的效果严格互逆）。
fn undo_insert(g: &mut Inner, id: [u8; 32], ordinal: u64, _newly_occupied: bool, key: &[u8]) {
    rollback_issue(&mut g.ledger, id, ordinal);
    let pl = place(key, &g.params);
    // 无论新占槽（copies 1->清空）还是重复（copies n->n-1），递减一次即可精确逆操作。
    match g.filter.decrement(id, pl.i1, pl.i2, pl.fingerprint) {
        Ok(d) => {
            if d.copies_before == 1 {
                g.counters.insert_ok = g.counters.insert_ok.saturating_sub(1);
            } else {
                g.counters.insert_dup = g.counters.insert_dup.saturating_sub(1);
            }
        }
        Err(e) => tracing::error!(error = %e, "插入回滚时未能递减副本（不应发生）"),
    }
}

/// 撤销一次从未交付的签发：issued 减一。
///
/// 正常持久化成功后 issued 单调不减；这里仅用于「签发已记账但快照落盘失败」的事务
/// 回滚——令牌从未返回给调用方，因此不构成序号复用。被回滚的序号必是最新且未花费。
fn rollback_issue(ledger: &mut Ledger, id: [u8; 32], ordinal: u64) {
    if let Some(st) = ledger.keys.get_mut(&id) {
        assert!(ordinal < st.issued, "回滚序号应在签发范围内");
        assert!(!spent_contains(st, ordinal), "回滚一个已花费序号会破坏记账");
        st.issued -= 1;
        let empty = st.issued == 0 && st.spent.iter().all(|w| *w == 0);
        if empty {
            ledger.keys.remove(&id);
        }
    }
}

fn spent_contains(st: &crate::ledger::KeyState, ordinal: u64) -> bool {
    let word = (ordinal / 64) as usize;
    st.spent
        .get(word)
        .is_some_and(|w| w & (1u64 << (ordinal % 64)) != 0)
}

fn b64(id: &[u8; 32]) -> String {
    use base64::Engine as _;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rollback_issue_keeps_spent_history() {
        let mut l = Ledger::new();
        let id = [1u8; 32];
        let o0 = l.record_issue(id);
        l.record_issue(id);
        l.mark_spent(id, o0);
        // 回滚最新（未花费）签发，issued 从 2 -> 1，花费历史保留。
        rollback_issue(&mut l, id, 1);
        assert_eq!(l.live_count(id), 0);
        assert_eq!(l.keys.get(&id).unwrap().issued, 1);
    }
}
