//! Build driver: deduplicates input, then retries the BDZ
//! peel-then-assign pipeline with a deterministic seed schedule until it
//! succeeds or the attempt budget is exhausted.

use super::assign::assign;
use super::graph::{build_edges, peel};
use super::index::MphIndex;
use crate::hash::fingerprint;

/// Load factor: vertices per key in the asymptotic regime. BDZ needs
/// c > ~1.22 for a peelable 3-uniform random hypergraph with high
/// probability.
pub const LOAD_FACTOR: f64 = 1.23;
/// Mid-size sets still need a higher ratio than the asymptotic value:
/// the peel-success threshold converges slowly. `n <= MID_SET_MAX`
/// uses 1.5 vertices per key.
pub const MID_FACTOR: f64 = 1.5;
pub const MID_SET_MAX: usize = 2048;
/// Small sets behave far worse than the asymptotic threshold predicts
/// (some tiny `(n, m)` pairs are structurally unpeelable), so they get a
/// generous 2n vertex budget.
pub const SMALL_SET_MAX: usize = 64;
/// Minimum vertex count so a single edge still has 3 distinct vertices.
pub const MIN_VERTICES: usize = 3;

/// Default cap on seed retries.
pub const DEFAULT_MAX_ATTEMPTS: u32 = 128;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum BuildError {
    #[error("peeling failed for all {attempts} seeded attempts (last: {last_failure})")]
    PeelingExhausted { attempts: u32, last_failure: String },
    #[error("input has {got} unique keys, limit is {limit}")]
    TooManyKeys { got: usize, limit: usize },
}

/// What a successful build produced, including diagnostics.
#[derive(Debug)]
pub struct BuildOutcome {
    pub index: MphIndex,
    /// Seed that finally worked (`base_seed + attempts - 1`).
    pub seed: u64,
    /// Number of seeded attempts used (>= 1).
    pub attempts: u32,
    /// Input keys dropped because they duplicated an earlier key.
    pub duplicates_removed: usize,
    /// Unique keys the index was built over, in first-seen order.
    pub unique_keys: Vec<Vec<u8>>,
}

/// Vertex count for `n` keys.
///
/// - `n == 0` -> 0 (empty index)
/// - `n <= 64` -> `max(3, 2n)` (small-set regime)
/// - `n <= 2048` -> `ceil(1.5 n)` (transition regime)
/// - otherwise -> `ceil(1.23 n)` (asymptotic regime)
pub fn vertex_count(n: usize) -> usize {
    if n == 0 {
        0
    } else if n <= SMALL_SET_MAX {
        (2 * n).max(MIN_VERTICES)
    } else if n <= MID_SET_MAX {
        (n as f64 * MID_FACTOR).ceil() as usize
    } else {
        (n as f64 * LOAD_FACTOR).ceil() as usize
    }
}

/// Seed for attempt `k` (0-based): a plain wrapping increment keeps the
/// schedule trivially reproducible across languages.
pub fn attempt_seed(base_seed: u64, attempt: u32) -> u64 {
    base_seed.wrapping_add(attempt as u64)
}

/// Deduplicate keys, preserving first-seen order.
pub fn dedup_keys(keys: &[Vec<u8>]) -> (Vec<Vec<u8>>, usize) {
    let mut seen = std::collections::HashSet::with_capacity(keys.len());
    let mut unique = Vec::with_capacity(keys.len());
    for k in keys {
        if seen.insert(k.clone()) {
            unique.push(k.clone());
        }
    }
    let removed = keys.len() - unique.len();
    (unique, removed)
}

/// Build an MPH index over `keys`.
///
/// Empty input yields a valid empty index. Duplicates are removed before
/// anything else. Retries are bounded by `max_attempts`.
pub fn build(
    keys: &[Vec<u8>],
    base_seed: u64,
    max_attempts: u32,
    key_limit: usize,
) -> Result<BuildOutcome, BuildError> {
    let (unique, duplicates_removed) = dedup_keys(keys);
    if unique.len() > key_limit {
        return Err(BuildError::TooManyKeys {
            got: unique.len(),
            limit: key_limit,
        });
    }
    let n = unique.len();
    if n == 0 {
        return Ok(BuildOutcome {
            index: MphIndex::empty(base_seed),
            seed: base_seed,
            attempts: 1,
            duplicates_removed,
            unique_keys: unique,
        });
    }
    let m = vertex_count(n);
    let mut last_failure = String::new();
    for attempt in 0..max_attempts.max(1) {
        let seed = attempt_seed(base_seed, attempt);
        let edges = match build_edges(seed, m, &unique) {
            Ok(e) => e,
            Err(d) => {
                last_failure = format!("degenerate edge at key index {}", d.key_index);
                continue;
            }
        };
        let stack = match peel(&edges, m) {
            Ok(s) => s,
            Err(f) => {
                last_failure = f.to_string();
                continue;
            }
        };
        let g = assign(&stack, &edges, m, n);
        // Fingerprints are indexed by *slot*, not by key order.
        let mut fps = vec![0u64; n];
        for key in &unique {
            let m64 = m as u64;
            let v0 = (crate::hash::vertex_hash(seed, 0, key) % m64) as usize;
            let v1 = (crate::hash::vertex_hash(seed, 1, key) % m64) as usize;
            let v2 = (crate::hash::vertex_hash(seed, 2, key) % m64) as usize;
            let slot = ((g[v0] + g[v1] + g[v2]) as u64 % n as u64) as usize;
            fps[slot] = fingerprint(seed, key);
        }
        return Ok(BuildOutcome {
            index: MphIndex::new(seed, n, m, g, fps),
            seed,
            attempts: attempt + 1,
            duplicates_removed,
            unique_keys: unique,
        });
    }
    Err(BuildError::PeelingExhausted {
        attempts: max_attempts.max(1),
        last_failure,
    })
}
