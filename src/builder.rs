//! Build pipeline: dedup -> seeded hyperedge generation -> peeling with a
//! bounded number of seeded retries -> assignment -> verifier material.
//!
//! The retry loop lives in [`build_core`] and is generic over a seed-scoped
//! [`EdgeSourceFactory`], so tests drive the *same* loop with scripted edge
//! sources (forced failure then success) rather than duplicating retry logic.

use std::sync::Arc;
use std::time::Instant;

use crate::error::{MphfError, Result};
use crate::hash;
use crate::index::{MphfIndex, VerifyMode};
use crate::kernel::{assign, peel, Assignment, EdgeSource, PeelOutcome};

/// Build parameters.
#[derive(Debug, Clone)]
pub struct BuildConfig {
    /// Target load factor n/m. For 3-hypergraph BDZ peeling the safe
    /// operating point is ~0.75-0.81; defaults to 0.75 for robust single-seed
    /// success on small sets (`m ≈ 1.34n`).
    pub load_factor: f64,
    /// First seed to try.
    pub base_seed: u64,
    /// Maximum attempts before reporting [`MphfError::Peel`]. Must be >= 1.
    pub max_attempts: u32,
    pub verify: VerifyMode,
    pub algorithm: u8,
}

impl Default for BuildConfig {
    fn default() -> Self {
        BuildConfig {
            load_factor: 0.75,
            base_seed: 1,
            max_attempts: 256,
            verify: VerifyMode::Fingerprint { bits: 16 },
            algorithm: crate::format::ALGO_BDZ3,
        }
    }
}

impl BuildConfig {
    pub fn validate(&self) -> Result<()> {
        if !(0.05..=0.81).contains(&self.load_factor) {
            return Err(MphfError::invalid_input(format!(
                "load_factor {} outside (0.05, 0.81]",
                self.load_factor
            )));
        }
        if self.max_attempts == 0 {
            return Err(MphfError::invalid_input("max_attempts must be >= 1"));
        }
        Ok(())
    }

    pub fn vertex_count(&self, n: usize) -> usize {
        // m is prime for the BDZ hashing analysis. A floor of 31 keeps both
        // the per-key edge-collision rate and the tiny-graph 3-core risk
        // negligible even for n <= ~25 (the index is still *minimal*:
        // ranking emits exactly n slots regardless of m).
        let target = (n as f64 / self.load_factor).ceil() as usize;
        next_prime(target.max(31))
    }
}

/// Smallest prime `>= x`. Trial division is ample here: `x` is at most a
/// small constant multiple of the keyset size, and primality of `m` is part
/// of the persistent index's structural assumptions.
pub fn next_prime(mut x: usize) -> usize {
    if x <= 2 {
        return 2;
    }
    if x.is_multiple_of(2) {
        x += 1;
    }
    while !is_prime(x) {
        x += 2;
    }
    x
}

fn is_prime(x: usize) -> bool {
    if x < 2 {
        return false;
    }
    if x.is_multiple_of(2) {
        return x == 2;
    }
    let mut d = 3usize;
    while d.saturating_mul(d) <= x {
        if x.is_multiple_of(d) {
            return false;
        }
        d += 2;
    }
    true
}

/// Outcome of one seeded attempt (for diagnostics).
#[derive(Debug, Clone, serde::Serialize)]
pub struct AttemptInfo {
    pub attempt: u32,
    pub seed: u64,
    pub result: String,
    /// Edges left in the core, when peeling failed.
    pub remaining: Option<usize>,
    pub elapsed_us: u128,
}

/// Result of a successful build.
#[derive(Debug, Clone)]
pub struct BuildReport {
    pub index: MphfIndex,
    /// Distinct keys actually indexed (`n`).
    pub distinct_keys: usize,
    /// Duplicate input rows dropped during dedup.
    pub duplicates_dropped: usize,
    pub seed: u64,
    pub attempts: u32,
    pub history: Vec<AttemptInfo>,
    pub elapsed_us: u128,
}

/// Factory producing an edge source for a specific seed. Implemented for the
/// real hash-based source; tests supply their own.
pub trait EdgeSourceFactory {
    type Source<'a>: EdgeSource
    where
        Self: 'a;
    fn make(&self, seed: u64) -> Self::Source<'_>;
    /// Original distinct keys (needed for verifier material), in input order.
    fn keys(&self) -> &[Vec<u8>];
}

/// Real edge source: BDZ vertices from [`hash::edge`].
pub struct HashEdgeSource {
    keys: Arc<[Vec<u8>]>,
    m: usize,
    seed: u64,
}

impl EdgeSource for HashEdgeSource {
    fn n(&self) -> usize {
        self.keys.len()
    }
    fn m(&self) -> usize {
        self.m
    }
    fn edge(&self, i: usize) -> Option<[u64; 3]> {
        hash::edge(&self.keys[i], self.seed, self.m as u64)
    }
}

/// Factory for real builds.
pub struct HashFactory {
    keys: Arc<[Vec<u8>]>,
    m: usize,
}

impl EdgeSourceFactory for HashFactory {
    type Source<'a> = HashEdgeSource;
    fn make(&self, seed: u64) -> HashEdgeSource {
        HashEdgeSource {
            keys: Arc::clone(&self.keys),
            m: self.m,
            seed,
        }
    }
    fn keys(&self) -> &[Vec<u8>] {
        &self.keys
    }
}

fn dedup(mut keys: Vec<Vec<u8>>) -> (Arc<[Vec<u8>]>, usize) {
    let total = keys.len();
    keys.sort();
    keys.dedup();
    let distinct = keys.len();
    (keys.into(), total - distinct)
}

fn edges_of<S: EdgeSource + ?Sized>(src: &S) -> Option<Vec<[u64; 3]>> {
    let mut edges = Vec::with_capacity(src.n());
    for i in 0..src.n() {
        edges.push(src.edge(i)?);
    }
    Some(edges)
}

fn finish(
    g: Vec<u8>,
    occupied: Vec<bool>,
    edges: Vec<[u64; 3]>,
    keys: &[Vec<u8>],
    seed: u64,
    cfg: &BuildConfig,
) -> Result<MphfIndex> {
    // `finish` re-validates the perfect map and builds rank/verifier state.
    MphfIndex::build(g, occupied, &edges, keys, seed, cfg.verify, cfg.algorithm)
}

/// The generic retry core. Returns the assigned table and edges on success.
pub fn build_core<F: EdgeSourceFactory>(
    factory: &F,
    cfg: &BuildConfig,
) -> Result<(MphfIndex, u64, u32, Vec<AttemptInfo>)> {
    cfg.validate()?;
    let keys = factory.keys();
    let n = keys.len();
    let m = cfg.vertex_count(n);

    let mut history = Vec::new();
    for attempt in 0..cfg.max_attempts {
        // Seeds must be independent across attempts. Splitting the base
        // seed into the mixing function ensures distinct attempts produce
        // uncorrelated hypergraphs (adjacent numeric seeds would share FNV
        // prefix structure, which is why tiny keysets can exhaust retries).
        let seed = if attempt == 0 {
            cfg.base_seed
        } else {
            hash::mix64(hash::mix64(cfg.base_seed).wrapping_add(
                (attempt as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15),
            ))
        };
        let t0 = Instant::now();
        let src = factory.make(seed);
        let (info, assigned): (AttemptInfo, Option<Assignment>) = match peel(&src) {
            PeelOutcome::Success(steps) => {
                let edges = edges_of(&src).expect("peel already resolved every edge");
                let a = assign(&edges, &steps, m);
                (
                    AttemptInfo {
                        attempt: attempt + 1,
                        seed,
                        result: "peeled".into(),
                        remaining: None,
                        elapsed_us: t0.elapsed().as_micros(),
                    },
                    Some(a),
                )
            }
            PeelOutcome::CoreRemain { remaining } => (
                AttemptInfo {
                    attempt: attempt + 1,
                    seed,
                    result: "core_remain".into(),
                    remaining: Some(remaining),
                    elapsed_us: t0.elapsed().as_micros(),
                },
                None,
            ),
            PeelOutcome::EdgeUnavailable => (
                AttemptInfo {
                    attempt: attempt + 1,
                    seed,
                    result: "edge_unavailable".into(),
                    remaining: None,
                    elapsed_us: t0.elapsed().as_micros(),
                },
                None,
            ),
        };

        if let Some(a) = assigned {
            // Re-resolve edges for the finishing step (source may be gone).
            let src2 = factory.make(seed);
            let edges = edges_of(&src2).expect("edge resolved during peel");
            match finish(a.g, a.occupied, edges, keys, seed, cfg) {
                Ok(idx) => {
                    history.push(info);
                    return Ok((idx, seed, attempt + 1, history));
                }
                // A map that peels but fails the bijection check is a defect,
                // not a retryable random event — surface it loudly.
                Err(e) => return Err(e),
            }
        }
        history.push(info);
    }

    Err(MphfError::Peel {
        message: format!(
            "peeling failed after {} seeded attempts (load_factor={})",
            cfg.max_attempts, cfg.load_factor
        ),
    })
}

/// Build from raw (possibly duplicate) byte-string keys.
pub fn build(keys: Vec<Vec<u8>>, cfg: &BuildConfig) -> Result<BuildReport> {
    let t0 = Instant::now();
    let (keys, duplicates_dropped) = dedup(keys);
    let n = keys.len();
    let m = cfg.vertex_count(n);
    let factory = HashFactory { keys, m };
    let (index, seed, attempts, history) = build_core(&factory, cfg)?;
    Ok(BuildReport {
        distinct_keys: index.key_count(),
        duplicates_dropped,
        index,
        seed,
        attempts,
        history,
        elapsed_us: t0.elapsed().as_micros(),
    })
}
