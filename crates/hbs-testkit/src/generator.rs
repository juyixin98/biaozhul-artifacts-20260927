//! Deterministic synthetic data generation.
//!
//! A fixed-seed SplitMix64-style PRNG (no `rand` dependency) feeds four
//! named distributions: sparse, dense, interleaved, and container-threshold
//! boundary cases. Every sample records the seed it came from so a failure
//! in a property test is fully reproducible from the log line.

/// Deterministic 64-bit PRNG (SplitMix64).
#[derive(Debug, Clone)]
pub struct Rng {
    state: u64,
}

impl Rng {
    pub fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform value in `[0, n)` (`n > 0`).
    pub fn below(&mut self, n: u64) -> u64 {
        self.next_u64() % n
    }

    pub fn u32_value(&mut self) -> u32 {
        self.next_u64() as u32
    }
}

/// Named synthetic distribution shapes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Distribution {
    /// Very few values spread across the universe; every chunk sparse.
    Sparse,
    /// Whole dense runs covering many complete chunks.
    Dense,
    /// Alternating sparse and dense chunks (interleaved).
    Interleaved,
    /// Chunks deliberately sized at / one below / one above the 4096
    /// switching threshold.
    ContainerThreshold,
}

impl Distribution {
    pub fn as_str(self) -> &'static str {
        match self {
            Distribution::Sparse => "sparse",
            Distribution::Dense => "dense",
            Distribution::Interleaved => "interleaved",
            Distribution::ContainerThreshold => "container_threshold",
        }
    }
}

/// One generated sample.
#[derive(Debug, Clone)]
pub struct Sample {
    /// Human-readable scenario name (distribution + seed).
    pub label: String,
    /// Sorted, unique values.
    pub values: Vec<u32>,
    /// Seed used; sufficient to regenerate the sample.
    pub seed: u64,
    /// Distribution shape.
    pub distribution: Distribution,
}

/// Generate a sample.
pub fn sample(distribution: Distribution, seed: u64) -> Sample {
    let mut rng = Rng::new(seed);
    let mut values: Vec<u32> = match distribution {
        Distribution::Sparse => {
            // ~200 values anywhere in u32.
            (0..200).map(|_| rng.u32_value()).collect()
        }
        Distribution::Dense => {
            // Three contiguous runs of ~30k values each.
            let mut v = Vec::new();
            for run in 0..3u32 {
                let base = run.wrapping_mul(1_500_000_000);
                for i in 0..30_000u32 {
                    v.push(base.wrapping_add(i));
                }
            }
            v
        }
        Distribution::Interleaved => {
            // 24 chunks; alternate sparse (~20 vals) and dense (~6000 vals).
            let mut v = Vec::new();
            for chunk in 0u32..24 {
                let base = chunk << 16;
                if chunk % 2 == 0 {
                    for _ in 0..20 {
                        v.push(base | (rng.below(65_536) as u32));
                    }
                } else {
                    // dense run within the chunk
                    for i in 0..6000u32 {
                        v.push(base | i);
                    }
                }
            }
            v
        }
        Distribution::ContainerThreshold => {
            // Three chunks sized exactly 4095, 4096, 4097 to exercise the
            // switching threshold from both sides. Even positions keep the
            // sets unique (8192 < 65536).
            let mut v = Vec::new();
            let sizes = [4095u32, 4096, 4097];
            for (chunk_idx, &count) in sizes.iter().enumerate() {
                let base = (chunk_idx as u32) << 16;
                for i in 0..count {
                    v.push(base | (i * 2));
                }
            }
            v
        }
    };

    values.sort_unstable();
    values.dedup();
    Sample {
        label: format!("{}-seed{}", distribution.as_str(), seed),
        values,
        seed,
        distribution,
    }
}

/// All four distributions at a fixed set of seeds: the reusable fixture set.
pub fn fixture_samples() -> Vec<Sample> {
    let mut out = Vec::new();
    for d in [
        Distribution::Sparse,
        Distribution::Dense,
        Distribution::Interleaved,
        Distribution::ContainerThreshold,
    ] {
        for seed in [1u64, 42, 777] {
            out.push(sample(d, seed));
        }
    }
    out
}
