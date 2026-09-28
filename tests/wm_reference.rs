//! Differential tests against a deliberately naive oracle:
//! the oracle sorts a copy of the window and scans linearly — it never
//! consults the kernel under test.

use wavelet_matrix_service::error::WmError;
use wavelet_matrix_service::index::WmIndex;

/// Deterministic xorshift so failures are reproducible without a `rand`
/// dependency.
struct XorShift(u64);

impl XorShift {
    fn new(seed: u64) -> Self {
        XorShift(seed | 1)
    }
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn between(&mut self, lo: i64, hi: i64) -> i64 {
        // half-open [lo, hi); spans here are small positive ranges
        let span = (hi - lo) as u64;
        lo + (self.next_u64() % span) as i64
    }
    fn pick(&mut self, choices: &[i64]) -> i64 {
        choices[(self.next_u64() as usize) % choices.len()]
    }
}

// ---------- naive reference implementations (the oracle) ----------

fn oracle_kth(data: &[i64], l: usize, r: usize, k: usize) -> i64 {
    let mut w: Vec<i64> = data[l..r].to_vec();
    w.sort_unstable();
    w[k]
}

fn oracle_count_lt(data: &[i64], l: usize, r: usize, bound: i64) -> usize {
    data[l..r].iter().filter(|&&v| v < bound).count()
}

fn oracle_count_range(data: &[i64], l: usize, r: usize, lo: i64, hi: i64) -> usize {
    data[l..r].iter().filter(|&&v| lo <= v && v < hi).count()
}

fn oracle_predecessor(data: &[i64], l: usize, r: usize, bound: i64) -> Option<i64> {
    data[l..r].iter().filter(|&&v| v < bound).max().copied()
}

fn oracle_successor(data: &[i64], l: usize, r: usize, bound: i64) -> Option<i64> {
    data[l..r].iter().filter(|&&v| v >= bound).min().copied()
}

fn oracle_bounds(data: &[i64]) -> Vec<i64> {
    let mut b: Vec<i64> = data
        .iter()
        .flat_map(|&v| [v.checked_sub(1), Some(v), v.checked_add(1)])
        .flatten()
        .collect();
    b.extend_from_slice(&[i64::MIN, i64::MAX, -1, 0, 1]);
    b.sort_unstable();
    b.dedup();
    b
}

/// Exhaustively compare every window/k and every interesting bound.
fn diff_against_oracle(data: &[i64]) {
    let idx = WmIndex::build(data).unwrap();
    let n = data.len();
    assert_eq!(idx.len(), n);
    let bounds = oracle_bounds(data);

    for l in 0..n {
        for r in (l + 1)..=n {
            // k-th smallest for every legal k
            for k in 0..(r - l) {
                assert_eq!(
                    idx.kth_smallest(l, r, k).unwrap(),
                    oracle_kth(data, l, r, k),
                    "kth mismatch: data={data:?} [{l},{r}) k={k}"
                );
            }
            for &bound in &bounds {
                assert_eq!(
                    idx.count_lt(l, r, bound).unwrap(),
                    oracle_count_lt(data, l, r, bound),
                    "count_lt mismatch: data={data:?} [{l},{r}) bound={bound}"
                );
                assert_eq!(
                    idx.predecessor(l, r, bound).unwrap(),
                    oracle_predecessor(data, l, r, bound),
                    "predecessor mismatch: data={data:?} [{l},{r}) bound={bound}"
                );
                assert_eq!(
                    idx.successor(l, r, bound).unwrap(),
                    oracle_successor(data, l, r, bound),
                    "successor mismatch: data={data:?} [{l},{r}) bound={bound}"
                );
            }
            // range counts over a spread of (lo, hi) pairs
            for &lo in bounds.iter().step_by(3) {
                for &hi in bounds.iter().step_by(5) {
                    assert_eq!(
                        idx.count_range(l, r, lo, hi).unwrap(),
                        oracle_count_range(data, l, r, lo, hi),
                        "count_range mismatch: data={data:?} [{l},{r}) [{lo},{hi})"
                    );
                }
            }
        }
    }
}

#[test]
fn random_small_value_domain_with_duplicates() {
    for seed in 0..40u64 {
        let mut rng = XorShift::new(0x9e37_79b9_7f4a_7c15 ^ seed);
        let n = 1 + (rng.next_u64() % 32) as usize;
        let data: Vec<i64> = (0..n).map(|_| rng.between(-4, 6)).collect();
        diff_against_oracle(&data);
    }
}

#[test]
fn random_medium_value_domain() {
    for seed in 0..30u64 {
        let mut rng = XorShift::new(0x2545_F491_4F6C_DD1Du64.wrapping_add(seed * 7919));
        let n = 1 + (rng.next_u64() % 40) as usize;
        let data: Vec<i64> = (0..n).map(|_| rng.between(-1000, 1001)).collect();
        diff_against_oracle(&data);
    }
}

#[test]
fn signed_extremes_and_bit_width_boundaries() {
    let extremes = [i64::MIN, i64::MIN + 1, -1, 0, 1, i64::MAX - 1, i64::MAX];
    for seed in 0..40u64 {
        let mut rng = XorShift::new(0xD1B5_4A4E_D1B5_4A4D ^ seed);
        let n = 1 + (rng.next_u64() % 36) as usize;
        let data: Vec<i64> = (0..n).map(|_| rng.pick(&extremes)).collect();
        diff_against_oracle(&data);
    }
}

#[test]
fn all_identical_values_including_extremes() {
    for &v in &[0i64, -1, 1, i64::MIN, i64::MAX] {
        for n in [1usize, 2, 17, 64, 100] {
            let data = vec![v; n];
            diff_against_oracle(&data);
        }
    }
}

#[test]
fn singleton_and_strictly_sorted_inputs() {
    diff_against_oracle(&[i64::MIN]);
    diff_against_oracle(&[i64::MAX]);
    diff_against_oracle(&[-5, -4, -3, -2, -1, 0, 1, 2, 3]);
    diff_against_oracle(&[3, 2, 1, 0, -1, -2, -3]);
}

#[test]
fn hand_computed_answers_on_a_fixed_array() {
    // This array also ships as fixtures/sample_arrays.json -> "mixed_small".
    let data = [5, -3, 7, 7, 0, -3, 42, 1];
    let idx = WmIndex::build(&data).unwrap();
    assert_eq!(idx.distinct(), 6);
    assert_eq!(idx.height(), 3);

    // sorted window: [-3, -3, 0, 1, 5, 7, 7, 42]
    assert_eq!(idx.kth_smallest(0, 8, 0).unwrap(), -3);
    assert_eq!(idx.kth_smallest(0, 8, 3).unwrap(), 1);
    assert_eq!(idx.kth_smallest(0, 8, 7).unwrap(), 42);

    // [2, 6) == [7, 7, 0, -3] -> sorted [-3, 0, 7, 7]
    assert_eq!(idx.kth_smallest(2, 6, 0).unwrap(), -3);
    assert_eq!(idx.kth_smallest(2, 6, 1).unwrap(), 0);
    assert_eq!(idx.kth_smallest(2, 6, 2).unwrap(), 7);
    assert_eq!(idx.kth_smallest(2, 6, 3).unwrap(), 7);

    assert_eq!(idx.count_lt(0, 8, 0).unwrap(), 2);
    assert_eq!(idx.count_lt(0, 8, 8).unwrap(), 7);
    assert_eq!(idx.count_range(0, 8, 0, 8).unwrap(), 5);
    assert_eq!(idx.count_range(0, 8, i64::MIN, i64::MAX).unwrap(), 8);

    assert_eq!(idx.predecessor(0, 8, 7).unwrap(), Some(5));
    assert_eq!(idx.predecessor(0, 8, -3).unwrap(), None);
    assert_eq!(idx.successor(0, 8, 7).unwrap(), Some(7));
    assert_eq!(idx.successor(0, 8, 43).unwrap(), None);
}

#[test]
fn invalid_inputs_are_rejected_with_specific_categories() {
    let data = [1, 2, 3, 4];
    let idx = WmIndex::build(&data).unwrap();

    assert_eq!(WmIndex::build(&[]).unwrap_err(), WmError::EmptyInput);

    // empty half-open window
    assert_eq!(
        idx.kth_smallest(2, 2, 0).unwrap_err(),
        WmError::EmptyRange { l: 2, r: 2 }
    );
    assert_eq!(
        idx.count_lt(2, 2, 10).unwrap_err(),
        WmError::EmptyRange { l: 2, r: 2 }
    );

    // window outside the sequence, or inverted
    assert_eq!(
        idx.kth_smallest(0, 5, 0).unwrap_err(),
        WmError::InvalidRange { l: 0, r: 5, len: 4 }
    );
    assert_eq!(
        idx.kth_smallest(3, 2, 0).unwrap_err(),
        WmError::InvalidRange { l: 3, r: 2, len: 4 }
    );

    // k out of bounds (0-based): window of length 3 allows k in 0..3
    assert_eq!(
        idx.kth_smallest(0, 3, 3).unwrap_err(),
        WmError::KOutOfBounds { k: 3, window: 3 }
    );
    assert_eq!(
        idx.kth_smallest(0, 3, 100).unwrap_err(),
        WmError::KOutOfBounds { k: 100, window: 3 }
    );

    // predecessor/successor honor the same window checks
    assert!(matches!(idx.predecessor(0, 4, 2), Ok(Some(1))));
    assert!(matches!(idx.successor(0, 4, 2), Ok(Some(2))));
}
