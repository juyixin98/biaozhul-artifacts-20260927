//! The queryable index: `g` array plus one 64-bit fingerprint per slot.
//!
//! A lookup computes the candidate slot from the hash triple and then
//! *verifies* the stored fingerprint against the query key's fingerprint.
//! Out-of-set keys therefore get an explicit `NotMember` decision with a
//! reason, never a silently wrong slot.

use crate::hash::{fingerprint, vertex_hash};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Lookup {
    /// Fingerprint matched at the candidate slot.
    Member { slot: u64 },
    /// Fingerprint mismatch (or empty index): key is not in the set.
    NotMember { reason: NotMemberReason },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NotMemberReason {
    EmptyIndex,
    FingerprintMismatch { candidate_slot: u64 },
}

#[derive(Debug, Clone)]
pub struct MphIndex {
    /// Seed that produced this index (bound into the persisted format).
    pub seed: u64,
    /// Number of keys (slots are `0..n`).
    pub n: usize,
    /// Number of graph vertices (`g.len()`).
    pub m: usize,
    /// Vertex values, each in `[0, n)`.
    pub g: Vec<u32>,
    /// Fingerprint of the key owning each slot.
    pub fps: Vec<u64>,
}

impl MphIndex {
    pub fn new(seed: u64, n: usize, m: usize, g: Vec<u32>, fps: Vec<u64>) -> Self {
        debug_assert_eq!(g.len(), m);
        debug_assert_eq!(fps.len(), n);
        Self { seed, n, m, g, fps }
    }

    pub fn empty(seed: u64) -> Self {
        Self::new(seed, 0, 0, Vec::new(), Vec::new())
    }

    /// Candidate slot for `key` without fingerprint verification.
    /// Only meaningful when `n > 0`.
    pub fn candidate_slot(&self, key: &[u8]) -> u64 {
        debug_assert!(self.n > 0);
        let m = self.m as u64;
        let v0 = (vertex_hash(self.seed, 0, key) % m) as usize;
        let v1 = (vertex_hash(self.seed, 1, key) % m) as usize;
        let v2 = (vertex_hash(self.seed, 2, key) % m) as usize;
        (self.g[v0] as u64 + self.g[v1] as u64 + self.g[v2] as u64) % self.n as u64
    }

    /// Verified lookup.
    pub fn lookup(&self, key: &[u8]) -> Lookup {
        if self.n == 0 {
            return Lookup::NotMember {
                reason: NotMemberReason::EmptyIndex,
            };
        }
        let slot = self.candidate_slot(key);
        if self.fps[slot as usize] == fingerprint(self.seed, key) {
            Lookup::Member { slot }
        } else {
            Lookup::NotMember {
                reason: NotMemberReason::FingerprintMismatch {
                    candidate_slot: slot,
                },
            }
        }
    }
}
