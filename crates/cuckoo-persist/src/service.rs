//! 服务状态：把索引内核、「凭证 -> 槽位」归属表、HMAC 主密钥与持久化绑定在一起。
//!
//! 关键语义：
//! - **插入**：内核先执行（迁移带轨迹）；成功后回放轨迹更新归属表，再原子落盘；
//!   若落盘失败，回滚内存（撤销指纹副本与归属变更），返回 [`PersistError::Storage`]，
//!   绝不出现“内存成功、磁盘丢失”的分裂状态。
//! - **删除**：必须先通过凭证签名校验且归属表中存在该 jti；
//!   只会删除凭证自己拥有的那个槽。一次性：删除后归属消失，重放得到明确错误。
//! - **查询**：纯近似判断，只读，永不报错地掩盖状态问题。

use std::collections::{HashMap, HashSet};
use std::path::Path;

use cuckoo_core::{
    credential, CoreError, CuckooFilter, FilterParams, InsertTrace, SplitMix64,
};

use crate::codec::{Snapshot, OWNER_LEN};
use crate::store::FileStore;

#[derive(Debug, thiserror::Error)]
pub enum PersistError {
    #[error(transparent)]
    Core(#[from] CoreError),
    #[error("存储层错误：{0}")]
    Storage(#[from] crate::store::StoreError),
    #[error("初始化错误：{0}")]
    Init(String),
}

pub type PersistResult<T> = Result<T, PersistError>;

/// 归属表中的凭证记录。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CredentialRecord {
    /// 插入时的指纹（删除槽位时二次核对）。
    pub fp: u32,
    /// 主候选桶 i1（仅用于观测/排查；定位槽位以 owners 网格为准）。
    pub i1: usize,
}

/// 插入结果（API 层直接序列化）。
#[derive(Debug, Clone)]
pub struct Inserted {
    pub token: String,
    pub jti: [u8; OWNER_LEN],
    pub kicks: usize,
    pub placed_bucket: usize,
    pub placed_slot: usize,
    pub load_factor: f64,
}

/// 删除结果。
#[derive(Debug, Clone)]
pub struct Removed {
    pub bucket: usize,
    pub slot: usize,
    pub load_factor: f64,
}

/// 查询观测信息。
#[derive(Debug, Clone)]
pub struct LookupInfo {
    pub member: bool,
    pub observed_copies: usize,
}

/// 统计信息（测试断言具体数字）。
#[derive(Debug, Clone, serde::Serialize)]
pub struct Stats {
    pub buckets: usize,
    pub bucket_size: u32,
    pub fingerprint_bits: u32,
    pub max_kicks: u32,
    pub total_slots: u64,
    pub occupied_slots: u64,
    pub load_factor: f64,
    pub live_credentials: usize,
    /// 存活副本数（同一键重复插入 n 次计 n；指纹近似使“不同键数”不可精确反推）。
    pub live_copies: usize,
}

/// 可持久化的过滤器服务。自身不持锁——API 层用单把 `Mutex<ServiceState>` 串行化，
/// 因此这里的“内核 + 归属表”更新永远不会撕裂。
pub struct ServiceState {
    params: FilterParams,
    filter: CuckooFilter,
    /// 与扁平槽位一一对应的 owner jti（全零表示空槽）。
    owners: Vec<[u8; OWNER_LEN]>,
    /// jti -> 凭证记录。与 owners 严格一一对应（同一 jti 全局唯一）。
    credentials: HashMap<[u8; OWNER_LEN], CredentialRecord>,
    master_key: Vec<u8>,
    rng: SplitMix64,
    store: FileStore,
    /// 逻辑插入次数（统计/日志用；重启后从快照重建）。
    generation: u64,
}

impl ServiceState {
    /// 首次创建（目标文件必须不存在，避免误覆盖已有数据）。
    pub fn create(
        path: impl AsRef<Path>,
        params: FilterParams,
        master_key: Vec<u8>,
        rng_seed: u64,
    ) -> PersistResult<Self> {
        params.validate()?;
        if master_key.len() < 16 {
            return Err(PersistError::Init(
                "HMAC 主密钥至少需要 16 字节".to_string(),
            ));
        }
        let store = FileStore::new(path.as_ref());
        if store.load()?.is_some() {
            return Err(PersistError::Init(format!(
                "{} 已存在快照；create 不会覆盖既有数据，请显式删除后再初始化",
                path.as_ref().display()
            )));
        }
        let total = params.total_slots() as usize;
        let filter = CuckooFilter::new(params)?;
        let mut svc = Self {
            params,
            filter,
            owners: vec![[0u8; OWNER_LEN]; total],
            credentials: HashMap::new(),
            master_key,
            rng: SplitMix64::new(rng_seed),
            store,
            generation: 0,
        };
        svc.flush()?;
        Ok(svc)
    }

    /// 打开已有快照，并校验调用方期望的参数/密钥与磁盘一致。
    pub fn open(
        path: impl AsRef<Path>,
        expected: FilterParams,
        master_key: Vec<u8>,
        rng_seed: u64,
    ) -> PersistResult<Self> {
        if master_key.len() < 16 {
            return Err(PersistError::Init(
                "HMAC 主密钥至少需要 16 字节".to_string(),
            ));
        }
        let store = FileStore::new(path.as_ref());
        let snap = store
            .load()?
            .ok_or_else(|| PersistError::Init(format!("{} 不存在，无法 open", path.as_ref().display())))?;

        if (
            snap.buckets_exp,
            snap.bucket_size,
            snap.fp_bits,
            snap.max_kicks,
        ) != (
            expected.buckets_exp,
            expected.bucket_size,
            expected.fingerprint_bits,
            expected.max_kicks,
        ) {
            return Err(PersistError::Init(format!(
                "参数与快照不一致：磁盘 buckets_exp={}, bucket_size={}, fp_bits={}, max_kicks={}",
                snap.buckets_exp, snap.bucket_size, snap.fp_bits, snap.max_kicks
            )));
        }
        expected.validate()?;

        let slots_u32: Vec<u32> = snap.slots.iter().map(|&x| x as u32).collect();
        let filter = CuckooFilter::from_slots(expected, slots_u32)?;
        let mut credentials = HashMap::new();
        // 归属表 -> 凭证账本（i1 无法从单槽反推，用 alt 关系从桶对推断；
        // 这里只需要 fp，i1 存 `min(bucket, alt)` 的表示以满足观测用途）。
        for (idx, jti) in snap.owners.iter().enumerate() {
            if *jti == [0u8; OWNER_LEN] {
                continue;
            }
            let fp = snap.slots[idx] as u32;
            let bucket = idx / expected.bucket_size as usize;
            let other = cuckoo_core::alt_index(bucket, fp, &expected);
            let i1 = bucket.min(other);
            credentials.insert(*jti, CredentialRecord { fp, i1 });
        }

        Ok(Self {
            params: expected,
            filter,
            owners: snap.owners,
            credentials,
            master_key,
            rng: SplitMix64::new(rng_seed),
            store,
            generation: 0,
        })
    }

    pub fn params(&self) -> &FilterParams {
        &self.params
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    fn snapshot(&self) -> Snapshot {
        Snapshot {
            buckets_exp: self.params.buckets_exp,
            bucket_size: self.params.bucket_size,
            fp_bits: self.params.fingerprint_bits,
            max_kicks: self.params.max_kicks,
            occupied: self.filter.occupied_slots(),
            slots: self
                .filter
                .slots()
                .iter()
                .map(|&fp| {
                    u16::try_from(fp).expect("fp_bits<=16，指纹可放入 u16")
                })
                .collect(),
            owners: self.owners.clone(),
        }
    }

    /// 强制刷盘（一般无需调用：每次变更后已自动落盘）。
    pub fn flush(&mut self) -> PersistResult<()> {
        Ok(self.store.save_atomic(&self.snapshot())?)
    }

    fn apply_trace_owners(&mut self, trace: &InsertTrace, jti: [u8; OWNER_LEN]) {
        let mut moving = jti;
        for sw in &trace.swaps {
            let off = sw.bucket * self.params.bucket_size as usize + sw.slot;
            std::mem::swap(&mut self.owners[off], &mut moving);
        }
        let poff = trace.placed.bucket * self.params.bucket_size as usize + trace.placed.slot;
        // 落点在 insert 中被保证为空：owner 必须是零。
        debug_assert_eq!(self.owners[poff], [0u8; OWNER_LEN]);
        self.owners[poff] = moving;
    }

    /// 插入一个键。重复键允许再次插入：每次插入都是独立副本 + 独立凭证，
    /// 副本计数 = 该键成功插入次数减去成功删除次数（详见 README）。
    pub fn insert(&mut self, key: &[u8]) -> PersistResult<Inserted> {
        let (fp, i1, i2) = cuckoo_core::locate(key, &self.params);

        // 内核先行（失败已内部回滚，无需清理）。
        let trace = match self.filter.insert(fp, i1, i2, &mut self.rng) {
            Ok(t) => t,
            Err(CoreError::FilterFull(kicks)) => {
                // 记录到 generation 之外：不改变任何持久状态。
                return Err(CoreError::FilterFull(kicks).into());
            }
            Err(e) => return Err(e.into()),
        };

        let jti = credential::new_jti();
        // jti 理论碰撞概率 2^-128；仍做显式防护，绝不复用。
        if self.credentials.contains_key(&jti) {
            // 尚未改动归属/凭证，只需撤销内核改动。
            self.filter.rollback_insert(&trace, fp);
            return Err(PersistError::Init("jti 冲突（2^-128 概率），请重试".into()));
        }

        self.apply_trace_owners(&trace, jti);

        // 先在内存建凭证记录，落盘失败时统一回滚。
        self.credentials
            .insert(jti, CredentialRecord { fp, i1 });
        self.generation += 1;

        if let Err(e) = self.flush() {
            // 落盘失败：撤销内存中本次插入的全部影响。
            self.credentials.remove(&jti);
            // 归属逆序回放：清空最终落点，携带其中 jti 反向交换；
            // 逆序结束后 carrying 即本次新 jti（被丢弃），所有旧归属归位。
            let poff =
                trace.placed.bucket * self.params.bucket_size as usize + trace.placed.slot;
            let mut carrying = std::mem::take(&mut self.owners[poff]);
            for sw in trace.swaps.iter().rev() {
                let off = sw.bucket * self.params.bucket_size as usize + sw.slot;
                std::mem::swap(&mut self.owners[off], &mut carrying);
            }
            debug_assert_eq!(carrying, jti);
            self.filter.rollback_insert(&trace, fp);
            self.generation -= 1;
            return Err(e);
        }

        Ok(Inserted {
            token: credential::issue(&self.master_key, &jti, key),
            jti,
            kicks: trace.swaps.len(),
            placed_bucket: trace.placed.bucket,
            placed_slot: trace.placed.slot,
            load_factor: self.filter.load_factor(),
        })
    }

    /// 撤销一次成功的内核插入的辅助已移入
    /// [`CuckooFilter::rollback_insert`]，服务层直接复用同一份逆序逻辑。
    /// 近似成员查询（只读，可能假阳性）。
    pub fn lookup(&self, key: &[u8]) -> LookupInfo {
        let (fp, i1, i2) = cuckoo_core::locate(key, &self.params);
        let observed_copies = self.filter.observed_copies(fp, i1, i2);
        LookupInfo {
            member: observed_copies > 0,
            observed_copies,
        }
    }

    /// 删除：凭证签名有效 + jti 当前归属某槽，才允许执行。
    pub fn delete(&mut self, key: &[u8], token: &str) -> PersistResult<Removed> {
        let jti = credential::verify(&self.master_key, token, key)?;

        let record = match self.credentials.get(&jti) {
            Some(r) => r.clone(),
            None => {
                // 签名有效但账本中不存在：已被消费，或根本不是本服务签发。
                return Err(CoreError::CredentialExhausted.into());
            }
        };

        // 在 owners 网格中找到该 jti 的唯一槽位（一一对应，线性扫描，
        // 规模为槽位数；对本项目规模足够，且避免维护第二份可变索引的不一致风险）。
        let pos = self
            .owners
            .iter()
            .position(|o| *o == jti)
            .ok_or_else(|| {
                PersistError::Init("账本有 jti 但归属表缺槽（状态损坏）".to_string())
            })?;
        let bucket = pos / self.params.bucket_size as usize;
        let slot = pos % self.params.bucket_size as usize;

        // 内核按“期望指纹”删除，任何不一致都显式报错。
        self.filter
            .remove_owned_slot(bucket, slot, record.fp)?;

        // 快照内存状态先改，再原子落盘；落盘失败则恢复并报错。
        let prev_owner = self.owners[pos];
        let rec_removed = self.credentials.remove(&jti);
        self.owners[pos] = [0u8; OWNER_LEN];
        self.generation += 1;

        if let Err(e) = self.flush() {
            self.owners[pos] = prev_owner;
            if let Some(r) = rec_removed {
                self.credentials.insert(jti, r);
            }
            // 恢复内核槽位（通过重新插入同指纹到同一槽的低层恢复点）。
            self.filter
                .restore_slot_for_rollback(bucket, slot, record.fp);
            self.generation -= 1;
            return Err(e);
        }

        Ok(Removed {
            bucket,
            slot,
            load_factor: self.filter.load_factor(),
        })
    }

    pub fn stats(&self) -> Stats {
        // 每个存活凭证对应一个存活副本；同一把钥匙重复插入会持有多个凭证。
        let live_credentials = self.credentials.len();
        Stats {
            buckets: self.params.num_buckets(),
            bucket_size: self.params.bucket_size,
            fingerprint_bits: self.params.fingerprint_bits,
            max_kicks: self.params.max_kicks,
            total_slots: self.params.total_slots(),
            occupied_slots: self.filter.occupied_slots(),
            load_factor: self.filter.load_factor(),
            live_credentials,
            live_copies: live_credentials,
        }
    }

    /// 仅供测试/验证：当前仍存活的 jti 集合。
    pub fn live_jtis(&self) -> HashSet<[u8; OWNER_LEN]> {
        self.credentials.keys().copied().collect()
    }

    /// 仅供测试/验证：用已知 jti 重新为同一把钥匙签 token（不改变账本）。
    pub fn token_for(&self, jti: &[u8; OWNER_LEN], key: &[u8]) -> Option<String> {
        self.credentials
            .contains_key(jti)
            .then(|| credential::issue(&self.master_key, jti, key))
    }
}
