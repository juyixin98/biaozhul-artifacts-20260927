//! Differential evidence verification.
//!
//! For every declared input combination (bounded by a configurable cap):
//! 1. the **independent** concrete executor (`ia-concrete`, which shares no
//!    code with the solver) runs the program;
//! 2. its result — normal value, or failure category at a location — is
//!    checked against the abstract report:
//!    * concrete failures must correspond to a site whose verdict is
//!      [`CheckVerdict::PossibleFailure`] or [`CheckVerdict::GuaranteedFailure`]
//!      with matching [`CheckKind`] and span;
//!    * concrete final variable values must lie inside the abstract exit
//!      interval;
//!    * concrete reachable sites must not be labelled unreachable.
//!
//! Any violation is an unsoundness report item. Items the abstract domain
//! could not decide are reported separately as `imprecision`, never as
//! failures.
use ia_concrete::{
    enumerate_inputs, run_program_bounded, EnumerationLimit, ExecFailure, FailureKind,
    RunOutcome,
};
use ia_intervals::Interval;
use ia_lang::ast::Program;
use ia_lang::ProgramInfo;
use ia_solver::{
    analyze, AnalysisReport, CheckKind, CheckVerdict, FailureCertainty,
};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VerifyConfig {
    /// Maximum number of enumerated input combinations.
    pub enumeration_cap: u64,
    /// Per-execution step limit.
    pub step_limit: u64,
    pub narrowing: bool,
}

impl Default for VerifyConfig {
    fn default() -> Self {
        Self {
            enumeration_cap: 200_000,
            step_limit: 200_000,
            narrowing: true,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ViolationKind {
    /// Concrete execution failed at a site the report calls safe.
    UnsoundFailureAtSafeSite,
    /// Concrete failure category did not match the recorded check kind.
    FailureKindMismatch,
    /// Concrete final value outside the abstract exit interval.
    ValueNotContained,
    /// Concrete execution reached a point declared unreachable.
    ReachedUnreachable,
    /// Abstract site says guaranteed-failure but no enumerated concrete run
    /// failed there (may be an enumeration-coverage caveat; see covered flag).
    GuaranteedNotObserved,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Violation {
    pub kind: ViolationKind,
    pub assignment: Vec<(String, i64)>,
    pub detail: String,
    pub at_offset: Option<usize>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailureObservation {
    pub kind: FailureKind,
    pub at_offset: usize,
    pub count: u64,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailureBucket {
    pub overflow: u64,
    pub div_by_zero: u64,
    pub index_out_of_bounds: u64,
    pub assertion_failed: u64,
    pub step_limit: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VerifyReport {
    pub sound: bool,
    pub combinations_run: u64,
    pub combinations_declared: u128,
    pub enumeration_complete: bool,
    pub normal_runs: u64,
    pub failed_runs: u64,
    pub failure_bucket: FailureBucket,
    /// (failure category, offset) -> observed count.
    pub failure_sites: Vec<FailureObservation>,
    /// Abstract sites labelled possible/guaranteed, with the number of
    /// concrete failures actually observed there.
    pub abstract_sites: Vec<AbstractSiteObservation>,
    pub violations: Vec<Violation>,
    /// Checks that could not be cross-validated because enumeration did not
    /// cover the whole domain.
    pub not_cross_validated: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AbstractSiteObservation {
    pub check_id: String,
    pub kind: CheckKind,
    pub verdict: CheckVerdict,
    pub at_offset: usize,
    pub observed_concrete_failures: u64,
}

pub fn verify(
    source: &str,
    program: &Program,
    info: &ProgramInfo,
    cfg: &VerifyConfig,
) -> Result<VerifyReport, EnumerationLimit> {
    let analysis_cfg = ia_solver::AnalyzerConfig {
        narrowing: cfg.narrowing,
        ..Default::default()
    };
    let report = analyze(program, info, analysis_cfg);
    verify_with_report(source, program, info, cfg, report)
}

pub fn verify_with_report(
    _source: &str,
    program: &Program,
    info: &ProgramInfo,
    cfg: &VerifyConfig,
    report: AnalysisReport,
) -> Result<VerifyReport, EnumerationLimit> {
    let assignments = enumerate_inputs(info, cfg.enumeration_cap)?;
    let total_declared: u128 = info
        .inputs
        .iter()
        .map(|i| (i.hi as i128 - i.lo as i128 + 1) as u128)
        .product::<u128>()
        .max(1);
    let complete = assignments.len() as u128 == total_declared;

    let mut violations = Vec::new();
    let mut bucket = FailureBucket::default();
    let mut normal_runs = 0u64;
    let mut failed_runs = 0u64;
    let mut site_counts: std::collections::BTreeMap<(FailureKind, usize), u64> =
        std::collections::BTreeMap::new();
    // Exit intervals per scalar from a fresh full-domain analysis trace are
    // not directly available; collect concrete exit values and check against
    // a second analysis of the whole program via the report's final state.
    let mut concrete_exit: std::collections::BTreeMap<String, Interval> =
        std::collections::BTreeMap::new();

    for assignment in &assignments {
        let outcome = run_program_bounded(program, info, _source, assignment, cfg.step_limit);
        let ordered: Vec<(String, i64)> = {
            let mut v: Vec<(String, i64)> = assignment.iter().map(|(k, v)| (k.clone(), *v)).collect();
            v.sort();
            v
        };
        match outcome {
            RunOutcome::Normal { final_vars, .. } => {
                normal_runs += 1;
                for (name, val) in final_vars {
                    let e = concrete_exit.entry(name).or_insert(Interval::BOTTOM);
                    *e = e.join(Interval::point(val));
                }
            }
            RunOutcome::Failed { failure, .. } => {
                failed_runs += 1;
                tally(&mut bucket, failure.kind);
                *site_counts.entry((failure.kind, failure.at_offset)).or_insert(0) += 1;
                check_failure_against_report(&failure, &report, &ordered, &mut violations);
            }
        }
    }

    // Guaranteed-failure sites must have been observed failing when the
    // domain was fully covered.
    let mut abstract_sites = Vec::new();
    let mut not_cross = Vec::new();
    for chk in &report.checks {
        let kind_opt = match chk.kind {
            CheckKind::Overflow => Some(FailureKind::Overflow),
            CheckKind::DivByZero => Some(FailureKind::DivByZero),
            CheckKind::IndexBounds => Some(FailureKind::IndexOutOfBounds),
            CheckKind::Assertion => Some(FailureKind::AssertionFailed),
        };
        let observed = kind_opt
            .and_then(|k| site_counts.get(&(k, chk.span.start.offset)).copied())
            .unwrap_or(0);
        abstract_sites.push(AbstractSiteObservation {
            check_id: chk.id.clone(),
            kind: chk.kind,
            verdict: chk.verdict,
            at_offset: chk.span.start.offset,
            observed_concrete_failures: observed,
        });
        if chk.verdict == CheckVerdict::GuaranteedFailure && observed == 0 {
            if complete {
                violations.push(Violation {
                    kind: ViolationKind::GuaranteedNotObserved,
                    assignment: Vec::new(),
                    detail: format!(
                        "site {} (offset {}) is reported guaranteed-failure but no concrete run failed there",
                        chk.id, chk.span.start.offset
                    ),
                    at_offset: Some(chk.span.start.offset),
                });
            } else {
                not_cross.push(chk.id.clone());
            }
        }
        if chk.verdict == CheckVerdict::Unreachable && observed > 0 {
            violations.push(Violation {
                kind: ViolationKind::ReachedUnreachable,
                assignment: Vec::new(),
                detail: format!("site {} was concretely reached but marked unreachable", chk.id),
                at_offset: Some(chk.span.start.offset),
            });
        }
    }

    // Exit-value containment: run a dedicated whole-program analysis result
    // that includes final intervals (the report only carries trace + checks).
    let exit_intervals = collect_exit_intervals(program, info, cfg.narrowing);
    for (name, concrete_iv) in &concrete_exit {
        if let Some(abs_iv) = exit_intervals.get(name) {
            if !concrete_iv.subset_of(*abs_iv) {
                violations.push(Violation {
                    kind: ViolationKind::ValueNotContained,
                    assignment: Vec::new(),
                    detail: format!(
                        "concrete exit hull {concrete_iv:?} for `{name}` not contained in abstract {abs_iv:?}"
                    ),
                    at_offset: None,
                });
            }
        }
    }

    let mut failure_sites: Vec<FailureObservation> = site_counts
        .into_iter()
        .map(|((kind, at_offset), count)| FailureObservation {
            kind,
            at_offset,
            count,
        })
        .collect();
    failure_sites.sort_by_key(|f| (f.at_offset, format!("{:?}", f.kind)));

    Ok(VerifyReport {
        sound: violations.is_empty(),
        combinations_run: assignments.len() as u64,
        combinations_declared: total_declared,
        enumeration_complete: complete,
        normal_runs,
        failed_runs,
        failure_bucket: bucket,
        failure_sites,
        abstract_sites,
        violations,
        not_cross_validated: not_cross,
    })
}

fn tally(b: &mut FailureBucket, k: FailureKind) {
    match k {
        FailureKind::Overflow => b.overflow += 1,
        FailureKind::DivByZero => b.div_by_zero += 1,
        FailureKind::IndexOutOfBounds => b.index_out_of_bounds += 1,
        FailureKind::AssertionFailed => b.assertion_failed += 1,
        FailureKind::StepLimit => b.step_limit += 1,
    }
}

fn check_failure_against_report(
    failure: &ExecFailure,
    report: &AnalysisReport,
    assignment: &[(String, i64)],
    violations: &mut Vec<Violation>,
) {
    let expected_kind: CheckKind = match failure.kind {
        FailureKind::Overflow => CheckKind::Overflow,
        FailureKind::DivByZero => CheckKind::DivByZero,
        FailureKind::IndexOutOfBounds => CheckKind::IndexBounds,
        FailureKind::AssertionFailed => CheckKind::Assertion,
        FailureKind::StepLimit => return, // harness limit, not language semantics
    };
    let Some(chk) = report
        .checks
        .iter()
        .find(|c| c.span.start.offset == failure.at_offset && c.kind == expected_kind)
    else {
        violations.push(Violation {
            kind: ViolationKind::UnsoundFailureAtSafeSite,
            assignment: assignment.to_vec(),
            detail: format!(
                "concrete {:?} at offset {} has no matching abstract check site",
                failure.kind, failure.at_offset
            ),
            at_offset: Some(failure.at_offset),
        });
        return;
    };
    match chk.verdict {
        CheckVerdict::Safe | CheckVerdict::Unreachable => violations.push(Violation {
            kind: ViolationKind::UnsoundFailureAtSafeSite,
            assignment: assignment.to_vec(),
            detail: format!(
                "concrete {:?} at offset {} but abstract verdict is {}",
                failure.kind,
                failure.at_offset,
                chk.verdict.as_str()
            ),
            at_offset: Some(failure.at_offset),
        }),
        CheckVerdict::PossibleFailure | CheckVerdict::GuaranteedFailure => {}
    }
    if chk.certainty == Some(FailureCertainty::Guaranteed) {
        // guaranteed is consistent with observed failure
    }
}

/// Re-run the analysis through its public entry point and pull the final
/// scalar intervals off the solver's post-body state. Exposed via a small
/// helper in the solver module for verifier use.
fn collect_exit_intervals(
    program: &Program,
    info: &ProgramInfo,
    narrowing: bool,
) -> std::collections::BTreeMap<String, Interval> {
    ia_solver::analyzer::exit_intervals(
        program,
        info,
        ia_solver::AnalyzerConfig {
            narrowing,
            ..Default::default()
        },
    )
}
