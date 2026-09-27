//! Independent test support — NOT part of the shipped crate.
#![allow(dead_code)]
//!
//! * [`caselog`]: per-case logging with a run identity, inputs, computation
//!   steps and the pass/fail verdict, so a failed run can be reproduced.
//! * [`rng`]: a tiny deterministic LCG (fixed seed → reproducible inputs).
//! * [`oracle`]: an entirely independent reference implementation. It stores
//!   point totals in a `HashMap` and answers rectangle sums by a **full scan**
//!   with plain `i128` arithmetic. It imports no `index`/`store`/`service`
//!   modules, so the expected answers cannot come from the kernel under test.

pub mod caselog {
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    static SEQ: AtomicU64 = AtomicU64::new(0);

    /// Process-wide run identity (timestamp + pid + counter).
    pub fn run_id() -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        let seq = SEQ.fetch_add(1, Ordering::SeqCst);
        format!("run-{nanos:016x}-pid{}-c{seq}", std::process::id())
    }

    /// Logger for one test case. Output goes to stderr so libtest
    /// captures it per case and shows it with `--nocapture`.
    pub struct CaseLog {
        run_id: String,
        case: String,
        steps: Vec<String>,
    }

    impl CaseLog {
        pub fn new(run_id: &str, case: impl Into<String>) -> Self {
            Self {
                run_id: run_id.to_string(),
                case: case.into(),
                steps: Vec::new(),
            }
        }

        /// Record one input fact or computation step.
        pub fn step(&mut self, what: impl std::fmt::Display) {
            self.steps.push(what.to_string());
        }

        fn dump(&self) {
            eprintln!("[{}] CASE {} BEGIN", self.run_id, self.case);
            for s in &self.steps {
                eprintln!("    step: {s}");
            }
        }

        /// Assert `cond`, dumping every recorded step on failure.
        pub fn assert_check(&mut self, cond: bool, verdict: impl std::fmt::Display) {
            if cond {
                eprintln!("[{}] CASE {} PASS ({verdict})", self.run_id, self.case);
            } else {
                self.dump();
                eprintln!("[{}] CASE {} FAIL — {verdict}", self.run_id, self.case);
                panic!("case {} failed: {verdict}", self.case);
            }
        }

        /// Unconditional failure with the full input/step trace.
        pub fn fail(self, verdict: impl std::fmt::Display) -> ! {
            self.dump();
            eprintln!("[{}] CASE {} FAIL — {verdict}", self.run_id, self.case);
            panic!("case {} failed: {verdict}", self.case);
        }
    }
}

pub mod rng {
    /// Deterministic 64-bit LCG (Numerical Recipes constants).
    #[derive(Debug, Clone)]
    pub struct Lcg {
        state: u64,
    }

    impl Default for Lcg {
        fn default() -> Self {
            // Fixed seed: identical inputs on every machine/run.
            Self {
                state: 0x1234_5678_9abc_def0,
            }
        }
    }

    impl Lcg {
        pub fn seeded(seed: u64) -> Self {
            Self {
                state: if seed == 0 { 0xdead_beef } else { seed },
            }
        }

        pub fn next_u64(&mut self) -> u64 {
            self.state = self
                .state
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            self.state
        }

        /// Uniform value in `[lo, hi]` inclusive; requires `hi >= lo`.
        pub fn range_i64(&mut self, lo: i64, hi: i64) -> i64 {
            assert!(hi >= lo, "empty range {lo}..{hi}");
            let span = (hi as i128) - (lo as i128) + 1;
            let pick = (self.next_u64() as i128).rem_euclid(span);
            (lo as i128 + pick) as i64
        }

        pub fn pick<'a, T>(&mut self, xs: &'a [T]) -> &'a T {
            &xs[(self.next_u64() as usize) % xs.len()]
        }

        pub fn chance(&mut self, p: f64) -> bool {
            let v = self.next_u64() >> 11; // 53 bits
            (v as f64 / (1u64 << 53) as f64) < p
        }
    }
}

pub mod oracle {
    //! Independent HashMap full-scan reference model (std only).

    use std::collections::{HashMap, HashSet};

    #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
    pub struct OPt {
        pub x: i64,
        pub y: i64,
    }

    #[derive(Debug, Clone, Copy)]
    pub struct OUpdate {
        pub x: i64,
        pub y: i64,
        pub delta: i64,
    }

    #[derive(Debug, Clone, Copy)]
    pub struct ORect {
        pub x_lo: i128,
        pub x_hi: i128,
        pub y_lo: i128,
        pub y_hi: i128,
    }

    #[derive(Debug, PartialEq, Eq)]
    pub enum OReject {
        Unregistered(i64, i64),
        DuplicateInBatch(i64, i64),
        EmptyBatch,
        Overflow(i64, i64, i128, i64),
        EmptyAxis(&'static str),
        NotInitialized,
        AlreadyInitialized,
    }

    #[derive(Debug, Clone, Default)]
    pub struct Snapshot {
        registered: HashSet<OPt>,
        totals: HashMap<OPt, i128>,
    }

    /// Full-scan reference with its own per-version history.
    #[derive(Debug, Clone, Default)]
    pub struct FullScanOracle {
        current: Snapshot,
        /// One snapshot per published version (`versions[0]` == v1).
        pub versions: Vec<Snapshot>,
    }

    impl FullScanOracle {
        pub fn new() -> Self {
            Self::default()
        }

        pub fn initialized(&self) -> bool {
            !self.versions.is_empty()
        }

        pub fn head_version(&self) -> u64 {
            self.versions.len() as u64
        }

        fn publish(&mut self) {
            self.versions.push(self.current.clone());
        }

        /// Initial registration (allowed only once).
        pub fn register(&mut self, xs: &[i64], ys: &[i64]) -> Result<u64, OReject> {
            if self.initialized() {
                return Err(OReject::AlreadyInitialized);
            }
            let xs = dedup(xs);
            let ys = dedup(ys);
            if xs.is_empty() {
                return Err(OReject::EmptyAxis("x"));
            }
            if ys.is_empty() {
                return Err(OReject::EmptyAxis("y"));
            }
            let mut registered = HashSet::new();
            for &x in &xs {
                for &y in &ys {
                    registered.insert(OPt { x, y });
                }
            }
            self.current = Snapshot {
                registered,
                totals: HashMap::new(),
            };
            self.publish();
            Ok(self.head_version())
        }

        /// Pre-validate and apply one batch atomically.
        pub fn batch(&mut self, updates: &[OUpdate]) -> Result<u64, OReject> {
            if !self.initialized() {
                return Err(OReject::NotInitialized);
            }
            if updates.is_empty() {
                return Err(OReject::EmptyBatch);
            }
            let mut keys: Vec<OPt> = updates.iter().map(|u| OPt { x: u.x, y: u.y }).collect();
            keys.sort_by_key(|p| (p.x, p.y));
            if let Some(w) = keys.windows(2).find(|w| w[0] == w[1]) {
                return Err(OReject::DuplicateInBatch(w[0].x, w[0].y));
            }
            for u in updates {
                if !self.current.registered.contains(&OPt { x: u.x, y: u.y }) {
                    return Err(OReject::Unregistered(u.x, u.y));
                }
            }
            // Compute candidates before mutating: rejection changes nothing.
            let mut candidates: Vec<(OPt, i128)> = Vec::with_capacity(updates.len());
            for u in updates {
                let p = OPt { x: u.x, y: u.y };
                let old = self.current.totals.get(&p).copied().unwrap_or(0);
                let new = old + u.delta as i128;
                if !(i64::MIN as i128..=i64::MAX as i128).contains(&new) {
                    return Err(OReject::Overflow(u.x, u.y, old, u.delta));
                }
                candidates.push((p, new));
            }
            for (p, v) in candidates {
                self.current.totals.insert(p, v);
            }
            self.publish();
            Ok(self.head_version())
        }

        /// Rebuild tables, carrying totals of surviving registered points.
        pub fn rebuild(&mut self, xs: &[i64], ys: &[i64]) -> Result<u64, OReject> {
            if !self.initialized() {
                return Err(OReject::NotInitialized);
            }
            let xs = dedup(xs);
            let ys = dedup(ys);
            if xs.is_empty() {
                return Err(OReject::EmptyAxis("x"));
            }
            if ys.is_empty() {
                return Err(OReject::EmptyAxis("y"));
            }
            let mut registered = HashSet::new();
            for &x in &xs {
                for &y in &ys {
                    registered.insert(OPt { x, y });
                }
            }
            let mut totals = HashMap::new();
            for (p, v) in &self.current.totals {
                if registered.contains(p) {
                    totals.insert(*p, *v);
                }
            }
            self.current = Snapshot { registered, totals };
            self.publish();
            Ok(self.head_version())
        }

        /// Full-scan inclusive-rectangle sum; also reports how many
        /// non-zero cells were walked (proves the scan really happened).
        pub fn query(&self, version: u64, r: ORect) -> (i128, usize, bool) {
            assert!(
                version >= 1 && (version as usize) <= self.versions.len(),
                "oracle: unknown version {version}"
            );
            if r.x_lo > r.x_hi || r.y_lo > r.y_hi {
                return (0, 0, true);
            }
            let snap = &self.versions[(version - 1) as usize];
            let mut sum = 0i128;
            let mut scanned = 0;
            for (p, v) in &snap.totals {
                if *v == 0 {
                    continue;
                }
                scanned += 1;
                let (x, y) = (p.x as i128, p.y as i128);
                if r.x_lo <= x && x <= r.x_hi && r.y_lo <= y && y <= r.y_hi {
                    sum += *v;
                }
            }
            (sum, scanned, false)
        }

        pub fn point_total(&self, version: u64, x: i64, y: i64) -> i128 {
            self.versions[(version - 1) as usize]
                .totals
                .get(&OPt { x, y })
                .copied()
                .unwrap_or(0)
        }

        pub fn xs(&self) -> Vec<i64> {
            let mut v: Vec<i64> = self.current.registered.iter().map(|p| p.x).collect();
            v.sort_unstable();
            v.dedup();
            v
        }

        pub fn ys(&self) -> Vec<i64> {
            let mut v: Vec<i64> = self.current.registered.iter().map(|p| p.y).collect();
            v.sort_unstable();
            v.dedup();
            v
        }
    }

    fn dedup(xs: &[i64]) -> Vec<i64> {
        let mut v = xs.to_vec();
        v.sort_unstable();
        v.dedup();
        v
    }
}
