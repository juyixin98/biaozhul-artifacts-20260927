//! Property checks comparing the SUT against the independent oracle.
//!
//! Each check returns a [`CheckReport`] describing the concrete failure
//! (including the probe value and both answers) rather than a bare boolean,
//! so every discrepancy is explainable in logs.

use hbs_core::HierBitmap;

use crate::oracle::ReferenceSet;

/// Outcome of one property batch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckReport {
    /// Number of individual assertions made.
    pub checks: usize,
    /// First discrepancy found, if any.
    pub failure: Option<CheckFailure>,
}

/// A concrete, reproducible discrepancy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckFailure {
    /// Which property failed.
    pub property: String,
    /// Human detail with both answers and the probe point.
    pub detail: String,
}

impl CheckReport {
    pub fn is_ok(&self) -> bool {
        self.failure.is_none()
    }
}

/// Build a SUT set from a value list (any order, duplicates allowed).
///
/// Sorts and deduplicates once, then uses the linear chunk-grouping
/// constructor — never n individual insertions.
pub fn sut_from(values: &[u32]) -> HierBitmap {
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    sorted.dedup();
    HierBitmap::from_sorted_unique(&sorted).expect("sorted+dedup is strictly increasing")
}

/// Full agreement battery between two value sets: cardinality, membership
/// probes, full ordering, rank/select with inversion identities, set algebra
/// against a second pair, and subset consistency.
pub fn cross_check(a: &[u32], b: &[u32]) -> CheckReport {
    let mut checks = 0usize;
    let mut assert = |cond: bool, property: &str, detail: String| -> Option<CheckFailure> {
        checks += 1;
        if cond {
            None
        } else {
            Some(CheckFailure {
                property: property.to_string(),
                detail,
            })
        }
    };

    let ref_a = ReferenceSet::from_values(a.iter().copied());
    let ref_b = ReferenceSet::from_values(b.iter().copied());
    let sut_a = sut_from(a);
    let sut_b = sut_from(b);

    // cardinality & ordered content
    if let Some(f) = assert(
        sut_a.len() == ref_a.len(),
        "cardinality",
        format!("sut={} oracle={}", sut_a.len(), ref_a.len()),
    ) {
        return CheckReport {
            checks,
            failure: Some(f),
        };
    }
    let sut_values: Vec<u32> = sut_a.iter().collect();
    if let Some(f) = assert(
        sut_values == ref_a.values_sorted(),
        "iter_sorted_unique",
        format!(
            "first divergence: {:?}",
            sut_values
                .iter()
                .zip(ref_a.values_sorted())
                .enumerate()
                .find(|(_, (x, y))| *x != y)
                .map(|(i, (x, y))| (i, *x, y))
        ),
    ) {
        return CheckReport {
            checks,
            failure: Some(f),
        };
    }

    // membership probes: every member, deterministically chosen non-members
    // (values adjacent to members and pseudo-random points), plus boundaries.
    let mut probes: Vec<u32> = a.to_vec();
    probes.extend(a.iter().take(300).map(|v| v.wrapping_add(1)));
    let mut rng = crate::Rng::new(0x00C0_FFEE);
    for _ in 0..500 {
        probes.push(rng.u32_value());
    }
    probes.extend([0, 1, 65535, 65536, u32::MAX - 1, u32::MAX]);
    for p in probes {
        if let Some(f) = assert(
            sut_a.contains(p) == ref_a.contains(p),
            "contains",
            format!(
                "probe={p} sut={} oracle={}",
                sut_a.contains(p),
                ref_a.contains(p)
            ),
        ) {
            return CheckReport {
                checks,
                failure: Some(f),
            };
        }
    }

    // rank/select agreement and inversion identities:
    //   select(rank_le(v)-1) == v  for every member v
    //   rank_lt(select(r))  == r   for every rank r in range
    let probe_ranks: Vec<u32> = a
        .iter()
        .step_by((a.len() / 500 + 1).max(1))
        .copied()
        .chain([0, u32::MAX])
        .collect();
    for v in probe_ranks {
        let r_sut = sut_a.rank_le(v);
        let r_ref = ref_a.rank_le(v);
        if let Some(f) = assert(
            r_sut == r_ref,
            "rank_le",
            format!("v={v} sut={r_sut} oracle={r_ref}"),
        ) {
            return CheckReport {
                checks,
                failure: Some(f),
            };
        }
        let r_sut = sut_a.rank_lt(v);
        let r_ref = ref_a.rank_lt(v);
        if let Some(f) = assert(
            r_sut == r_ref,
            "rank_lt",
            format!("v={v} sut={r_sut} oracle={r_ref}"),
        ) {
            return CheckReport {
                checks,
                failure: Some(f),
            };
        }
    }
    // Inversion identities on sampled members and on every rank for small
    // sets.
    let step = (ref_a.len() as usize / 200 + 1).max(1);
    for r in (0..ref_a.len()).step_by(step) {
        let sel_sut = sut_a.select(r);
        let sel_ref = ref_a.select(r);
        if let Some(f) = assert(
            sel_sut == sel_ref,
            "select",
            format!("rank={r} sut={sel_sut:?} oracle={sel_ref:?}"),
        ) {
            return CheckReport {
                checks,
                failure: Some(f),
            };
        }
        if let Some(v) = sel_sut {
            let back = sut_a.rank_lt(v);
            if let Some(f) = assert(
                back == r,
                "select_rank_inverse",
                format!("select({r})={v}, rank_lt={back}"),
            ) {
                return CheckReport {
                    checks,
                    failure: Some(f),
                };
            }
        }
    }
    // rank_le(select) = r+1
    for &v in a.iter().take(300).chain(a.iter().rev().take(300)) {
        let r = ref_a.rank_lt(v);
        if let Some(f) = assert(
            sut_a.rank_le(v) == r + 1,
            "rank_le_after_select",
            format!("v={v} rank_lt={r} rank_le={}", sut_a.rank_le(v)),
        ) {
            return CheckReport {
                checks,
                failure: Some(f),
            };
        }
    }

    // algebra
    let cases = [
        ("union", sut_a.union(&sut_b), ref_a.union(&ref_b)),
        (
            "intersection",
            sut_a.intersection(&sut_b),
            ref_a.intersection(&ref_b),
        ),
        (
            "difference",
            sut_a.difference(&sut_b),
            ref_a.difference(&ref_b),
        ),
        (
            "symmetric_difference",
            sut_a.symmetric_difference(&sut_b),
            ref_a.symmetric_difference(&ref_b),
        ),
    ];
    for (name, sut_res, ref_res) in cases {
        let sut_vals: Vec<u32> = sut_res.iter().collect();
        let ref_vals = ref_res.values_sorted();
        if let Some(f) = assert(
            sut_res.len() == ref_res.len() && sut_vals == ref_vals,
            name,
            format!(
                "len sut={} oracle={}; content_equal={}",
                sut_res.len(),
                ref_res.len(),
                sut_vals == ref_vals
            ),
        ) {
            return CheckReport {
                checks,
                failure: Some(f),
            };
        }
    }

    if let Some(f) = assert(
        sut_a.is_subset(&sut_b) == ref_a.is_subset(&ref_b),
        "is_subset",
        format!(
            "sut={} oracle={}",
            sut_a.is_subset(&sut_b),
            ref_a.is_subset(&ref_b)
        ),
    ) {
        return CheckReport {
            checks,
            failure: Some(f),
        };
    }

    CheckReport {
        checks,
        failure: None,
    }
}
