//! 每键记账：已签发（插入）数量与已花费（删除）序号集合。
//!
//! 与概率性的 Cuckoo 桶表不同，这一层是**精确**的，它使删除授权满足：
//!
//! * 重复插入同一键：[`Ledger::record_issue`] 返回递增的插入序号 0,1,2…，每张令牌唯一。
//! * 删除前必须持有与「键 + 序号」匹配且未使用的令牌（[`Ledger::check_spend`]）。
//! * 每个序号只能花费一次：重放、伪造序号、删除从未插入的键，都得到明确错误类别。
//! * 当前存活计数 = issued − spent；为 0 时该键视为已不存在（可重新插入，序号继续递增，
//!   已花费的旧序号永不复活）。
//!
//! 服务层据此保证：删除只会移除「确实由成功插入产生」的指纹，不会因过滤器假阳性
//! 而删除其他键的数据。

use serde::{Deserialize, Serialize};

use crate::credentials::CredentialError;

/// 一个键的记账状态。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KeyState {
    /// 已签发令牌数（成功插入次数）；单调不减。
    pub issued: u64,
    /// 已花费序号的位图，第 k 位对应序号 k。稀疏存储，按需增长。
    pub spent: Vec<u64>,
}

impl KeyState {
    fn new() -> Self {
        Self {
            issued: 0,
            spent: Vec::new(),
        }
    }

    /// 当前存活副本数。
    pub fn live(&self) -> u64 {
        let spent_count: u64 = self.spent.iter().map(|w| w.count_ones() as u64).sum();
        self.issued - spent_count
    }

    fn is_spent(&self, ordinal: u64) -> bool {
        let word = (ordinal / 64) as usize;
        let bit = ordinal % 64;
        self.spent.get(word).is_some_and(|w| w & (1u64 << bit) != 0)
    }

    fn mark_spent(&mut self, ordinal: u64) {
        let word = (ordinal / 64) as usize;
        if self.spent.len() <= word {
            self.spent.resize(word + 1, 0);
        }
        self.spent[word] |= 1u64 << (ordinal % 64);
    }

    /// 撤销花费（持久化失败时回滚删除）。
    fn unmark_spent(&mut self, ordinal: u64) {
        let word = (ordinal / 64) as usize;
        if let Some(w) = self.spent.get_mut(word) {
            *w &= !(1u64 << (ordinal % 64));
        }
    }
}

/// 记账本。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ledger {
    /// 以键标识（见 [`crate::token::key_id`]）为主键。
    /// JSON 对象键必须是字符串，因此持久化时序列化为 64 位十六进制；内存中仍是数组。
    #[serde(serialize_with = "ser_keys", deserialize_with = "de_keys")]
    pub keys: std::collections::BTreeMap<[u8; 32], KeyState>,
}

mod key_map_serde {
    use super::KeyState;
    use serde::{Deserializer, Serializer};
    use std::collections::BTreeMap;

    pub(crate) fn serialize<S: Serializer>(
        map: &BTreeMap<[u8; 32], KeyState>,
        s: S,
    ) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeMap;
        let mut out = s.serialize_map(Some(map.len()))?;
        for (k, v) in map {
            out.serialize_entry(&hex::encode(k), v)?;
        }
        out.end()
    }

    pub(crate) fn deserialize<'de, D: Deserializer<'de>>(
        d: D,
    ) -> Result<BTreeMap<[u8; 32], KeyState>, D::Error> {
        let raw: BTreeMap<String, KeyState> = serde::Deserialize::deserialize(d)?;
        let mut out = BTreeMap::new();
        for (hk, v) in raw {
            let bytes = hex::decode(&hk).map_err(serde::de::Error::custom)?;
            if bytes.len() != 32 {
                return Err(serde::de::Error::custom(format!(
                    "键标识长度 {} != 32",
                    bytes.len()
                )));
            }
            let mut id = [0u8; 32];
            id.copy_from_slice(&bytes);
            out.insert(id, v);
        }
        Ok(out)
    }
}
use key_map_serde::{deserialize as de_keys, serialize as ser_keys};

impl Ledger {
    pub fn new() -> Self {
        Self::default()
    }

    /// 记录一次成功插入，返回该令牌序号。
    pub fn record_issue(&mut self, id: [u8; 32]) -> u64 {
        let st = self.keys.entry(id).or_insert_with(KeyState::new);
        let ordinal = st.issued;
        st.issued += 1;
        ordinal
    }

    /// 校验删除凭证：键存在、序号已签发、尚未花费。
    /// 不修改状态（修改发生在 [`Ledger::mark_spent`]）。
    pub fn check_spend(&self, id: [u8; 32], ordinal: u64) -> Result<(), CredentialError> {
        let st = self.keys.get(&id).ok_or(CredentialError::UnknownKey)?;
        if ordinal >= st.issued {
            return Err(CredentialError::OrdinalNeverIssued);
        }
        if st.is_spent(ordinal) {
            return Err(CredentialError::TokenReplayed);
        }
        Ok(())
    }

    /// 标记序号已花费（校验通过后调用）。
    pub fn mark_spent(&mut self, id: [u8; 32], ordinal: u64) {
        self.keys
            .get_mut(&id)
            .expect("check_spend 已保证键存在")
            .mark_spent(ordinal);
    }

    /// 撤销一次花费（删除事务回滚用）。
    pub fn unmark_spent(&mut self, id: [u8; 32], ordinal: u64) {
        if let Some(st) = self.keys.get_mut(&id) {
            st.unmark_spent(ordinal);
        }
    }

    /// 某键当前存活副本数（0 也可能是从未插入，调用方自行区分）。
    pub fn live_count(&self, id: [u8; 32]) -> u64 {
        self.keys.get(&id).map_or(0, KeyState::live)
    }

    /// 存活键的数量（live > 0 的不同键）。
    pub fn live_keys(&self) -> usize {
        self.keys.values().filter(|s| s.live() > 0).count()
    }

    /// 总存活副本数。
    pub fn total_live(&self) -> u64 {
        self.keys.values().map(KeyState::live).sum()
    }

    /// 已签发令牌总数。
    pub fn total_issued(&self) -> u64 {
        self.keys.values().map(|s| s.issued).sum()
    }

    /// 不变式自检：每个被标记花费的序号都必须 < issued。
    pub fn verify_invariants(&self) -> Result<(), String> {
        for (id, st) in &self.keys {
            for (wi, word) in st.spent.iter().enumerate() {
                for bit in 0..64u32 {
                    if word & (1u64 << bit) != 0 {
                        let ordinal = wi as u64 * 64 + bit as u64;
                        if ordinal >= st.issued {
                            return Err(format!(
                                "键 {} 序号 {ordinal} 被标记花费但从未签发（issued={}）",
                                hex_short(id),
                                st.issued
                            ));
                        }
                    }
                }
            }
        }
        Ok(())
    }
}

fn hex_short(id: &[u8; 32]) -> String {
    use base64::Engine as _;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(&id[..9])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn duplicate_insert_counts_and_replay_is_blocked() {
        let mut l = Ledger::new();
        let id = [3u8; 32];
        assert_eq!(l.record_issue(id), 0);
        assert_eq!(l.record_issue(id), 1);
        assert_eq!(l.record_issue(id), 2);
        assert_eq!(l.live_count(id), 3);

        l.check_spend(id, 1).unwrap();
        l.mark_spent(id, 1);
        assert_eq!(l.live_count(id), 2);

        assert_eq!(
            l.check_spend(id, 1).unwrap_err(),
            CredentialError::TokenReplayed
        );
        assert_eq!(
            l.check_spend(id, 5).unwrap_err(),
            CredentialError::OrdinalNeverIssued
        );
        assert_eq!(
            l.check_spend([9u8; 32], 0).unwrap_err(),
            CredentialError::UnknownKey
        );

        // 删完后重新插入，序号继续递增，旧已花费序号不复活。
        l.mark_spent(id, 0);
        l.mark_spent(id, 2);
        assert_eq!(l.live_count(id), 0);
        assert_eq!(l.record_issue(id), 3);
        assert_eq!(l.live_count(id), 1);
        assert_eq!(
            l.check_spend(id, 0).unwrap_err(),
            CredentialError::TokenReplayed
        );
        l.check_spend(id, 3).unwrap();
    }
}
