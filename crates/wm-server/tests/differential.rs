//! High-volume differential test: hundreds of independently seeded random
//! arrays. The oracle sorts copies and performs linear scans; the kernel is
//! never used to generate expected values.

use wm_core::WaveletMatrix;

struct SplitMix64(u64);
impl SplitMix64 {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        z ^ (z >> 31)
    }
    fn below(&mut self, m: u64) -> u64 {
        if m == 0 {
            0
        } else {
            self.next() % m
        }
    }
}

fn oracle_quantile(v: &[i64], l: usize, r: usize, k: u64) -> i64 {
    let mut s: Vec<i64> = v[l..r].to_vec();
    s.sort_unstable();
    s[k as usize]
}

fn oracle_count(v: &[i64], l: usize, r: usize, lo: i64, hi: i64) -> u64 {
    v[l..r].iter().filter(|&&x| lo <= x && x < hi).count() as u64
}

#[test]
fn differential_stress_across_widths() {
    let mut rng = SplitMix64(0xdead_beef_cafe_f00d);
    // Fixed, seeded (reproducible) sweep.
    for seed in 0..250u64 {
        rng.0 ^= seed.wrapping_mul(0x1234_5678_9abc_def0);
        let n = 1 + rng.below(64) as usize;
        let width = seed % 5;
        let v: Vec<i64> = (0..n)
            .map(|_| match width {
                0 => (rng.below(3) as i64) - 1, // tiny universe, heavy duplicates
                1 => (rng.next() as u8) as i64,
                2 => (rng.next() as u16) as i64,
                3 => (rng.next() as i32) as i64,
                _ => match rng.below(16) {
                    0 => i64::MIN,
                    1 => i64::MAX,
                    _ => rng.next() as i64,
                },
            })
            .collect();

        let wm = WaveletMatrix::build(&v).unwrap();

        // Full-range quantiles.
        let mut sorted_full = v.clone();
        sorted_full.sort_unstable();
        for (k, expected) in sorted_full.iter().enumerate() {
            assert_eq!(
                wm.quantile(0, n, k as u64).unwrap(),
                *expected,
                "seed={seed} width={width} full k={k} v={v:?}"
            );
        }

        // Random sub-ranges and counts.
        for _ in 0..12 {
            let l = rng.below(n as u64) as usize;
            let r = l + 1 + rng.below((n - l) as u64) as usize;
            let k = rng.below((r - l) as u64);
            assert_eq!(
                wm.quantile(l, r, k).unwrap(),
                oracle_quantile(&v, l, r, k),
                "seed={seed} sub [{l},{r}) k={k} v={v:?}"
            );

            let mut pick = || -> i64 {
                match rng.below(8) {
                    0 => i64::MIN,
                    1 => i64::MAX,
                    2 => -1,
                    3 => 0,
                    4 => 1,
                    _ => v[rng.below(n as u64) as usize],
                }
            };
            let mut lo = pick();
            let mut hi = pick();
            if lo > hi {
                std::mem::swap(&mut lo, &mut hi);
            }
            let got = wm.range_count(l, r, lo, hi).unwrap();
            let want = oracle_count(&v, l, r, lo, hi);
            assert_eq!(got, want, "seed={seed} count [{lo},{hi}) v={v:?}");

            // Neighbors checked by linear scan.
            let x = pick();
            let pred = wm.predecessor(l, r, x).unwrap();
            let succ = wm.successor(l, r, x).unwrap();
            assert_eq!(pred.value, v[l..r].iter().copied().filter(|&z| z < x).max());
            assert_eq!(succ.value, v[l..r].iter().copied().filter(|&z| z > x).min());
            assert_eq!(pred.present, v[l..r].contains(&x));
        }
    }
}

#[test]
fn diagnostic_traces_are_consistent_with_answers() {
    let v = [40i64, -1, 40, 7, 0, -1, 100, 7];
    let wm = WaveletMatrix::build(&v).unwrap();

    // Sub-range [1,7) = [-1,40,7,0,-1,100], sorted [-1,-1,0,7,40,100].
    let qt = wm.explain_quantile(1, 7, 2).unwrap();
    assert_eq!(qt.value, wm.quantile(1, 7, 2).unwrap());
    assert_eq!(qt.value, 0);
    assert_eq!(qt.query_l, 1);
    assert_eq!(qt.query_r, 7);
    // Each level must carry a non-decreasing depth and real intervals.
    for (i, step) in qt.steps.iter().enumerate() {
        assert_eq!(step.depth, i);
        assert!(step.range_before.0 <= step.range_before.1);
        assert!(step.range_after.0 <= step.range_after.1);
        assert!(step.chosen_bit == 0 || step.chosen_bit == 1);
    }
    // Reconstructing the id from chosen bits must end at the returned id.
    let mut rebuilt = 0u64;
    for s in &qt.steps {
        if s.chosen_bit == 1 {
            rebuilt |= 1u64 << s.bit_position;
        }
    }
    assert_eq!(rebuilt, qt.id);

    let ct = wm.explain_range_count(0, 8, -1, 40).unwrap();
    assert_eq!(ct.count, wm.range_count(0, 8, -1, 40).unwrap());
    // [-1, 40): -1 twice, 0 once, 7 twice; 40 and 100 excluded by open bound.
    assert_eq!(ct.count, 5);
    assert_eq!(ct.below_hi.count - ct.below_lo.as_ref().unwrap().count, 5);

    // Reversed value range is explicitly flagged, not silently computed.
    let rev = wm.explain_range_count(0, 8, 40, -1).unwrap();
    assert_eq!(rev.count, 0);
    assert_eq!(rev.note, Some("EMPTY_OR_REVERSED_VALUE_RANGE"));

    let pt = wm.explain_predecessor(0, 8, 7).unwrap();
    assert_eq!(pt.value, Some(0));
    assert!(pt.present);
    assert!(pt.selection.is_some());
    let st = wm.explain_successor(0, 8, 7).unwrap();
    assert_eq!(st.value, Some(40));
    assert!(st.present);
}
