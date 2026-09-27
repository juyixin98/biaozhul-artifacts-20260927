//! In-memory index: the minimal perfect map plus the membership verifier
//! (fingerprint table or full original keys).

use crate::error::{MphfError, Result};
use crate::hash;

/// Membership verification strategy.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VerifyMode {
    /// Store a `bits`-bit fingerprint per slot. Tunable false-positive rate.
    Fingerprint { bits: u8 },
    /// Store every original key and compare exactly. Zero false positives.
    FullKey,
}

impl VerifyMode {
    pub fn parse(bits: u8) -> Result<VerifyMode> {
        match bits {
            0 => Ok(VerifyMode::FullKey),
            8 | 16 | 32 | 64 => Ok(VerifyMode::Fingerprint { bits }),
            other => Err(MphfError::invalid_input(format!(
                "fingerprint bits must be 0 (full key) or 8/16/32/64, got {other}"
            ))),
        }
    }

    pub fn bits(self) -> u8 {
        match self {
            VerifyMode::Fingerprint { bits } => bits,
            VerifyMode::FullKey => 0,
        }
    }
}

/// Rank-support structure over a packed bit vector: `rank1(i) = sum_{j<i} bit[j]`.
///
/// Block granularity is one 64-bit word; a query touches one cumulative
/// count plus one `count_ones`, so O(1) time with ~12.5% overhead.
#[derive(Debug, Clone)]
pub struct BitRank {
    bits: Vec<u64>,
    /// `cum[w]` = number of set bits strictly before word `w`.
    cum: Vec<u64>,
    len: usize,
}

impl BitRank {
    pub fn from_bits(bits: &[bool]) -> BitRank {
        let words = bits.len().div_ceil(64);
        let mut raw = vec![0u64; words];
        for (i, &b) in bits.iter().enumerate() {
            if b {
                raw[i / 64] |= 1u64 << (i % 64);
            }
        }
        BitRank::from_raw(raw, bits.len())
    }

    pub fn from_raw(raw: Vec<u64>, len: usize) -> BitRank {
        let mut cum = Vec::with_capacity(raw.len() + 1);
        let mut total: u64 = 0;
        for w in &raw {
            cum.push(total);
            total += w.count_ones() as u64;
        }
        cum.push(total);
        BitRank {
            bits: raw,
            cum,
            len,
        }
    }

    #[inline]
    pub fn get(&self, i: usize) -> bool {
        i < self.len && (self.bits[i / 64] >> (i % 64)) & 1 == 1
    }

    /// Number of set bits at positions `< i`.
    #[inline]
    pub fn rank1(&self, i: u64) -> u64 {
        let i = i as usize;
        if i == 0 {
            return 0;
        }
        let i = i.min(self.len);
        let w = i / 64;
        let masked = self.bits[w] & ((1u64 << (i % 64)) - 1);
        self.cum[w] + masked.count_ones() as u64
    }

    pub fn total_ones(&self) -> u64 {
        *self.cum.last().unwrap_or(&0)
    }
}

/// The immutable minimal perfect hash over one fixed set of byte-string keys.
#[derive(Debug, Clone)]
pub struct MphfIndex {
    pub n: usize,
    pub m: usize,
    pub seed: u64,
    pub algo: u8,
    pub mode: VerifyMode,
    g_packed: Vec<u8>,
    /// Occupancy bitset, 8 vertices per byte.
    occ_packed: Vec<u8>,
    rank: [BitRank; 3],
    /// Fingerprint table (fixed-width entries) when in fingerprint mode.
    fps: Vec<u8>,
    /// Concatenated keys + `n+1` offsets in full-key mode.
    keys_blob: Vec<u8>,
    key_offsets: Vec<u64>,
}

impl MphfIndex {
    /// Assemble an index from the assigned `g` table, occupancy bitset and
    /// original keys.
    ///
    /// # Errors
    /// Returns an error if the inputs do not form a minimal perfect map:
    /// occupancy size != n, a selector lands on an unoccupied vertex, or
    /// slots are not a permutation of 0..n.
    pub fn build(
        g: Vec<u8>,
        occupied: Vec<bool>,
        edges: &[[u64; 3]],
        keys: &[Vec<u8>],
        seed: u64,
        mode: VerifyMode,
        algo: u8,
    ) -> Result<MphfIndex> {
        let n = keys.len();
        let m = g.len();
        if edges.len() != n {
            return Err(MphfError::Internal(format!(
                "edge count {} != key count {}",
                edges.len(),
                n
            )));
        }
        if occupied.len() != m {
            return Err(MphfError::Internal(format!(
                "occupancy length {} != m {m}",
                occupied.len()
            )));
        }
        if occupied.iter().filter(|o| **o).count() != n {
            return Err(MphfError::Internal(format!(
                "occupancy has {} vertices, expected {n}",
                occupied.iter().filter(|o| **o).count()
            )));
        }

        // g classes restricted to occupied vertices:
        // G_i = {occupied v : g(v)==i}; |G_0|+|G_1|+|G_2| = n.
        let mut selected = vec![u64::MAX; n];
        let mut vertex_owner = vec![usize::MAX; m];
        for (i, e) in edges.iter().enumerate() {
            let s = ((g[e[0] as usize] + g[e[1] as usize] + g[e[2] as usize]) % 3) as usize;
            let v = e[s];
            if !occupied[v as usize] {
                return Err(MphfError::Internal(format!(
                    "key {i} selects unoccupied vertex {v}"
                )));
            }
            if vertex_owner[v as usize] != usize::MAX {
                return Err(MphfError::Internal(format!(
                    "keys {} and {} share selected vertex {v} (collision)",
                    vertex_owner[v as usize],
                    i
                )));
            }
            vertex_owner[v as usize] = i;
            selected[i] = v;
        }

        let b0: Vec<bool> = (0..m).map(|v| occupied[v] && g[v] == 0).collect();
        let b1: Vec<bool> = (0..m).map(|v| occupied[v] && g[v] == 1).collect();
        let b2: Vec<bool> = (0..m).map(|v| occupied[v] && g[v] == 2).collect();
        let rank = [
            BitRank::from_bits(&b0),
            BitRank::from_bits(&b1),
            BitRank::from_bits(&b2),
        ];

        // The rank class is the *selected vertex's own g value* (which
        // equals the selector only incidentally); the selector's job is to
        // find the vertex, not to pick the rank offset.
        let slot_for = |v: u64| -> u64 {
            match g[v as usize] {
                0 => rank[0].rank1(v),
                1 => rank[0].total_ones() + rank[1].rank1(v),
                _ => rank[0].total_ones() + rank[1].total_ones() + rank[2].rank1(v),
            }
        };

        // Compute slots and prove they are a permutation of 0..n.
        let mut seen_slot = vec![usize::MAX; n.max(1)];
        for (i, e) in edges.iter().enumerate() {
            let v = selected[i];
            let _s = ((g[e[0] as usize] + g[e[1] as usize] + g[e[2] as usize]) % 3) as usize;
            let slot = slot_for(v);
            if slot >= n as u64 {
                return Err(MphfError::Internal(format!(
                    "slot {slot} out of range at key {i}"
                )));
            }
            if seen_slot[slot as usize] != usize::MAX {
                return Err(MphfError::Internal(format!(
                    "slot {} shared by keys {} and {}",
                    slot,
                    seen_slot[slot as usize],
                    i
                )));
            }
            seen_slot[slot as usize] = i;
        }

        // Pack g, 4 vertices per byte, low-two-bits first.
        let mut g_packed = vec![0u8; m.div_ceil(4)];
        for (i, &gv) in g.iter().enumerate() {
            g_packed[i / 4] |= (gv & 3) << ((i % 4) * 2);
        }
        // Pack occupancy, 8 vertices per byte.
        let mut occ_packed = vec![0u8; m.div_ceil(8)];
        for (i, &on) in occupied.iter().enumerate() {
            if on {
                occ_packed[i / 8] |= 1u8 << (i % 8);
            }
        }

        // Membership material, laid out in *slot* order.
        // First derive slot per key.
        let mut slot_of = vec![0u64; n];
        for (i, _e) in edges.iter().enumerate() {
            let v = selected[i];
            slot_of[i] = match g[v as usize] {
                0 => rank[0].rank1(v),
                1 => rank[0].total_ones() + rank[1].rank1(v),
                _ => rank[0].total_ones() + rank[1].total_ones() + rank[2].rank1(v),
            };
        }

        let (fps, keys_blob, key_offsets) = match mode {
            VerifyMode::Fingerprint { bits } => {
                let width = (bits / 8) as usize;
                let mut table = vec![0u8; n * width];
                for (i, key) in keys.iter().enumerate() {
                    let fp = hash::fingerprint(key, seed, bits);
                    let off = slot_of[i] as usize * width;
                    table[off..off + width].copy_from_slice(&fp.to_le_bytes()[..width]);
                }
                (table, Vec::new(), Vec::new())
            }
            VerifyMode::FullKey => {
                // Keys in slot order; offsets has n+1 entries.
                let mut ordered: Vec<(u64, &Vec<u8>)> =
                    keys.iter().enumerate().map(|(i, k)| (slot_of[i], k)).collect();
                ordered.sort_by_key(|(s, _)| *s);
                let mut blob = Vec::new();
                let mut offsets = Vec::with_capacity(n + 1);
                offsets.push(0u64);
                for (_, k) in ordered {
                    blob.extend_from_slice(k);
                    offsets.push(blob.len() as u64);
                }
                (Vec::new(), blob, offsets)
            }
        };

        Ok(MphfIndex {
            n,
            m,
            seed,
            algo,
            mode,
            g_packed,
            occ_packed,
            rank,
            fps,
            keys_blob,
            key_offsets,
        })
    }

    #[inline]
    fn g(&self, v: usize) -> u8 {
        (self.g_packed[v / 4] >> ((v % 4) * 2)) & 3
    }

    #[inline]
    fn occupied(&self, v: usize) -> bool {
        (self.occ_packed[v / 8] >> (v % 8)) & 1 == 1
    }

    /// Selector for a query key: selected vertex, occupancy, and slot.
    fn map_key(&self, key: &[u8]) -> Result<(u64, bool, [u64; 3])> {
        if self.n == 0 {
            // No key can be a member of the empty set.
            return Ok((u64::MAX, false, [0; 3]));
        }
        let e = hash::edge(key, self.seed, self.m as u64)
            .ok_or_else(|| MphfError::Internal("edge unavailable for query key".into()))?;
        let s = ((self.g(e[0] as usize) + self.g(e[1] as usize) + self.g(e[2] as usize)) % 3)
            as usize;
        let v = e[s];
        let is_occupied = self.occupied(v as usize);
        // Rank within the selected vertex's own g-class.
        let slot = match self.g(v as usize) {
            0 => self.rank[0].rank1(v),
            1 => self.rank[0].total_ones() + self.rank[1].rank1(v),
            _ => self.rank[0].total_ones() + self.rank[1].total_ones() + self.rank[2].rank1(v),
        };
        Ok((slot, is_occupied, e))
    }

    fn fp_at(&self, slot: u64) -> u64 {
        let width = match self.mode {
            VerifyMode::Fingerprint { bits } => (bits / 8) as usize,
            VerifyMode::FullKey => unreachable!(),
        };
        let off = slot as usize * width;
        let mut bytes = [0u8; 8];
        bytes[..width].copy_from_slice(&self.fps[off..off + width]);
        u64::from_le_bytes(bytes)
    }

    fn key_at(&self, slot: u64) -> &[u8] {
        let lo = self.key_offsets[slot as usize] as usize;
        let hi = self.key_offsets[slot as usize + 1] as usize;
        &self.keys_blob[lo..hi]
    }

    /// Probe a candidate key.
    pub fn probe(&self, key: &[u8]) -> Probe {
        if self.n == 0 {
            return Probe::Rejected {
                reason: RejectReason::EmptySet,
                slot: None,
            };
        }
        let (slot, is_occupied, _edge) = match self.map_key(key) {
            Ok(v) => v,
            Err(_) => {
                // The key cannot even form a distinct hyperedge under this
                // index's seed, so it cannot be one of the member keys.
                return Probe::Rejected {
                    reason: RejectReason::EdgeCollision,
                    slot: None,
                };
            }
        };
        if !is_occupied || slot >= self.n as u64 {
            // Selector landed on a vertex no key owns: provably a non-member.
            return Probe::Rejected {
                reason: RejectReason::Unoccupied,
                slot: Some(slot),
            };
        }
        match self.mode {
            VerifyMode::Fingerprint { bits } => {
                let stored = self.fp_at(slot);
                let got = hash::fingerprint(key, self.seed, bits);
                if stored == got {
                    Probe::Member { slot }
                } else {
                    Probe::Rejected {
                        reason: RejectReason::FingerprintMismatch,
                        slot: Some(slot),
                    }
                }
            }
            VerifyMode::FullKey => {
                if self.key_at(slot) == key {
                    Probe::Member { slot }
                } else {
                    Probe::Rejected {
                        reason: RejectReason::KeyMismatch,
                        slot: Some(slot),
                    }
                }
            }
        }
    }

    pub fn key_count(&self) -> usize {
        self.n
    }
    pub fn vertex_count(&self) -> usize {
        self.m
    }
    pub fn g_packed(&self) -> &[u8] {
        &self.g_packed
    }
    pub fn occ_packed(&self) -> &[u8] {
        &self.occ_packed
    }
    pub fn rank_bits(&self) -> [&BitRank; 3] {
        [&self.rank[0], &self.rank[1], &self.rank[2]]
    }
    pub fn fingerprints(&self) -> &[u8] {
        &self.fps
    }
    pub fn keys_blob(&self) -> &[u8] {
        &self.keys_blob
    }
    pub fn key_offsets(&self) -> &[u64] {
        &self.key_offsets
    }

    /// Reconstruct from decoded components (used by the format loader).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn from_parts(
        n: usize,
        m: usize,
        seed: u64,
        algo: u8,
        mode: VerifyMode,
        g_packed: Vec<u8>,
        occ_packed: Vec<u8>,
        fps: Vec<u8>,
        keys_blob: Vec<u8>,
        key_offsets: Vec<u64>,
    ) -> MphfIndex {
        let mut b0 = vec![false; m];
        let mut b1 = vec![false; m];
        let mut b2 = vec![false; m];
        for v in 0..m {
            if (occ_packed[v / 8] >> (v % 8)) & 1 == 0 {
                continue; // unoccupied: ranks nowhere regardless of g
            }
            let gv = (g_packed[v / 4] >> ((v % 4) * 2)) & 3;
            match gv {
                0 => b0[v] = true,
                1 => b1[v] = true,
                2 => b2[v] = true,
                other => panic!("occupied vertex {v} has invalid g={other}"),
            }
        }
        MphfIndex {
            n,
            m,
            seed,
            algo,
            mode,
            g_packed,
            occ_packed,
            rank: [
                BitRank::from_bits(&b0),
                BitRank::from_bits(&b1),
                BitRank::from_bits(&b2),
            ],
            fps,
            keys_blob,
            key_offsets,
        }
    }
}

/// Result of a membership probe.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Probe {
    /// Accepted: key is a member and assigned this slot in `0..n`.
    Member { slot: u64 },
    /// Rejected: the key is provably not a member (verifier disagreed, or
    /// the set is empty).
    Rejected {
        reason: RejectReason,
        /// Candidate slot the perfect map mapped the key to (diagnostic).
        slot: Option<u64>,
    },
    /// Cannot decide: index corrupt in a way that makes mapping undefined.
    Inconclusive { reason: &'static str },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RejectReason {
    FingerprintMismatch,
    KeyMismatch,
    /// Selector selected a vertex no member key owns.
    Unoccupied,
    /// Key's hashes collide to fewer than 3 distinct vertices under the
    /// bound seed; members never do (build only succeeds on valid edges).
    EdgeCollision,
    EmptySet,
}

impl RejectReason {
    pub fn as_str(self) -> &'static str {
        match self {
            RejectReason::FingerprintMismatch => "fingerprint_mismatch",
            RejectReason::KeyMismatch => "key_mismatch",
            RejectReason::Unoccupied => "unoccupied_vertex",
            RejectReason::EdgeCollision => "edge_collision",
            RejectReason::EmptySet => "empty_set",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bitrank_brute_force() {
        let bits: Vec<bool> = (0..1000u32).map(|i| i % 3 == 0).collect();
        let br = BitRank::from_bits(&bits);
        let mut acc = 0u64;
        for i in 0..=bits.len() {
            assert_eq!(br.rank1(i as u64), acc);
            if i < bits.len() && bits[i] {
                acc += 1;
            }
        }
        assert_eq!(br.total_ones(), (1000 + 2) / 3);
    }
}
