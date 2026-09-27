//! Shared support for integration tests.
//!
//! `mod common` provides:
//! - a tiny deterministic PRNG for generating *probe* keys (fixture keys
//!   themselves come from the Python oracle's fixture);
//! - scripted edge sources for driving the real retry core into forced
//!   peeling failures/successes;
//! - JSON fixture loading.
#![allow(dead_code)]

use std::sync::Arc;

use mphf::builder::EdgeSourceFactory;
use mphf::kernel::EdgeSource;

/// Deterministic xorshift64* — only generates test inputs, never expected
/// answers.
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Rng(seed | 1)
    }
    pub fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    pub fn gen_key(&mut self) -> Vec<u8> {
        let n = 6 + (self.next_u64() % 20) as usize;
        (0..n).map(|_| b'a' + (self.next_u64() % 26) as u8).collect()
    }
}

/// Edge source returning a caller-provided edge list, with per-seed override
/// capability used to force peel failures at chosen attempts.
pub struct ScriptedSource {
    pub edges: Arc<Vec<[u64; 3]>>,
    pub m: usize,
}

impl EdgeSource for ScriptedSource {
    fn n(&self) -> usize {
        self.edges.len()
    }
    fn m(&self) -> usize {
        self.m
    }
    fn edge(&self, i: usize) -> Option<[u64; 3]> {
        Some(self.edges[i])
    }
}

/// Factory whose edge list is chosen per attempt via a closure. This drives
/// the *production* retry loop (`build_core`) with deterministic failures.
pub struct ScriptedFactory {
    pub keys: Arc<[Vec<u8>]>,
    pub m: usize,
    /// Edges per attempt index; attempt past the end reuses the last entry.
    pub per_attempt: Arc<Vec<Vec<[u64; 3]>>>,
    pub counter: std::sync::atomic::AtomicUsize,
}

impl EdgeSourceFactory for ScriptedFactory {
    type Source<'a> = ScriptedSource;
    fn make(&self, _seed: u64) -> ScriptedSource {
        // The caller cannot observe which attempt the core is on directly,
        // but make() is called exactly once per attempt; emulate with an
        // atomic counter inside the factory.
        let idx = self
            .counter
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst)
            .min(self.per_attempt.len() - 1);
        ScriptedSource {
            edges: Arc::new(self.per_attempt[idx].clone()),
            m: self.m,
        }
    }
    fn keys(&self) -> &[Vec<u8>] {
        &self.keys
    }
}

// Extend the struct above with the counter field (kept adjacent so the test
// helpers read as one unit).
impl ScriptedFactory {
    pub fn new(
        keys: Arc<[Vec<u8>]>,
        m: usize,
        per_attempt: Vec<Vec<[u64; 3]>>,
    ) -> Self {
        ScriptedFactory {
            keys,
            m,
            per_attempt: Arc::new(per_attempt),
            counter: std::sync::atomic::AtomicUsize::new(0),
        }
    }
}

/// The complete K4^3 hypergraph on four vertices: four edges, every vertex
/// degree 3 — an immediate non-empty 3-core, peeling cannot start.
pub fn k4_core_edges() -> Vec<[u64; 3]> {
    vec![[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]]
}

/// Two disjoint edges on six vertices: peelable in two steps.
pub fn disjoint_edges() -> Vec<[u64; 3]> {
    vec![[0, 1, 2], [3, 4, 5]]
}

pub fn load_fixture() -> serde_json::Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/golden.json");
    let text = std::fs::read_to_string(path).expect("golden.json fixture missing");
    serde_json::from_str(&text).expect("golden.json invalid")
}
