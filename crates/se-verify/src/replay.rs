//! Counterexample replay against the independent concrete interpreter.

use std::collections::BTreeMap;

use se_lang::interp::{self, Failure, FailureKind, FlowOutcome, RunOpts};
use se_lang::Program;
use se_engine::report::AnalysisReport;
use se_engine::Evidence;
use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum ReplayStatus {
    /// Replay failed exactly where and how the engine predicted.
    Confirmed,
    /// Replay did not corroborate the candidate (any mismatch is a real problem).
    Rejected,
}

impl ReplayStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            ReplayStatus::Confirmed => "confirmed",
            ReplayStatus::Rejected => "rejected",
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct VerifiedEvidence {
    pub status: String,
    pub engine: Evidence,
    /// Actual replay outcome category: completed / infeasible_assume / failure name.
    pub replay_outcome: String,
    pub replay_stmt: Option<usize>,
    pub replay_steps: u64,
    /// Final store after replay (for inspection), masked inputs first.
    pub replay_store: BTreeMap<String, u64>,
    pub reason: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct VerifiedReport {
    pub engine_verdict: String,
    /// Final public verdict after evidence corroboration.
    pub final_verdict: String,
    pub verified: Vec<VerifiedEvidence>,
    pub confirmed_count: usize,
    pub rejected_count: usize,
}

/// Normalize an engine model into the declared input domains:
/// * missing inputs get their lower bound;
/// * values outside the width or declared domains are rejected (the engine was
///   supposed to clamp them; masking here would hide a solver bug);
/// * models binding names the program never declares are rejected.
pub fn normalize_inputs(
    program: &Program,
    model: &BTreeMap<String, u64>,
) -> Result<BTreeMap<String, u64>, String> {
    let mut out = BTreeMap::new();
    for decl in &program.inputs {
        let raw = model.get(&decl.name).copied().unwrap_or(decl.low);
        if raw > program.width.mask_u64() {
            return Err(format!(
                "input '{}' value {raw} exceeds {}-bit range",
                decl.name,
                program.width.bits()
            ));
        }
        if !(decl.low..=decl.high).contains(&raw) {
            return Err(format!(
                "input '{}' value {raw} outside declared domain [{},{}]",
                decl.name, decl.low, decl.high
            ));
        }
        out.insert(decl.name.clone(), raw);
    }
    for k in model.keys() {
        if !program.is_input(k) {
            return Err(format!("model binds undeclared input '{k}'"));
        }
    }
    Ok(out)
}

/// Replay one piece of evidence.
pub fn verify_evidence(
    program: &Program,
    evidence: &Evidence,
    opts: RunOpts,
) -> VerifiedEvidence {
    let inputs = match normalize_inputs(program, &evidence.inputs) {
        Ok(i) => i,
        Err(e) => {
            return VerifiedEvidence {
                status: ReplayStatus::Rejected.as_str().to_string(),
                engine: evidence.clone(),
                replay_outcome: "invalid_input".to_string(),
                replay_stmt: None,
                replay_steps: 0,
                replay_store: BTreeMap::new(),
                reason: Some(e),
            }
        }
    };

    let result = interp::run(program, &inputs, opts.clone());
    let replay_store = result.final_store.clone();

    let (outcome_name, replay_stmt, reason) = match &result.outcome {
        FlowOutcome::Failed(Failure {
            kind,
            stmt_id,
            op,
        }) => {
            let same_kind = *kind == evidence.failure;
            let same_stmt = *stmt_id == evidence.stmt_id;
            if same_kind && same_stmt {
                (
                    kind.as_str().to_string(),
                    Some(*stmt_id),
                    None,
                )
            } else {
                (
                    kind.as_str().to_string(),
                    Some(*stmt_id),
                    Some(format!(
                        "replay failed as {} at stmt {stmt_id}{}; engine predicted {} at stmt {}",
                        kind.as_str(),
                        op.map(|o| format!(" op {o}")).unwrap_or_default(),
                        evidence.failure.as_str(),
                        evidence.stmt_id
                    )),
                )
            }
        }
        FlowOutcome::Completed => (
            "completed".to_string(),
            None,
            Some("counterexample replay completed without failure".to_string()),
        ),
        FlowOutcome::InfeasibleAssume => (
            "infeasible_assume".to_string(),
            None,
            Some("counterexample replay was cut by an assume".to_string()),
        ),
    };

    let confirmed = reason.is_none();
    VerifiedEvidence {
        status: if confirmed {
            ReplayStatus::Confirmed.as_str().to_string()
        } else {
            ReplayStatus::Rejected.as_str().to_string()
        },
        engine: evidence.clone(),
        replay_outcome: outcome_name,
        replay_stmt,
        replay_steps: result.steps,
        replay_store,
        reason,
    }
}

/// Verify every candidate in an engine report and compute the final verdict.
///
/// Verdict policy:
/// * engine `violation` + at least one confirmed witness → `violation`;
/// * engine `violation` but no confirmed witness → `unknown` (evidence uncorroborated);
/// * engine `holds`/`unknown` are preserved (holds requires zero cuts/unknowns, which
///   the engine already guarantees).
pub fn verify_report(
    program: &Program,
    report: &AnalysisReport,
    opts: RunOpts,
) -> VerifiedReport {
    let verified: Vec<VerifiedEvidence> = report
        .evidence
        .iter()
        .map(|e| verify_evidence(program, e, opts.clone()))
        .collect();
    let confirmed_count = verified
        .iter()
        .filter(|v| v.status == ReplayStatus::Confirmed.as_str())
        .count();
    let rejected_count = verified.len() - confirmed_count;

    let final_verdict = match report.verdict.as_str() {
        "violation" => {
            if confirmed_count > 0 {
                "violation"
            } else {
                "unknown"
            }
        }
        other => other,
    }
    .to_string();

    VerifiedReport {
        engine_verdict: report.verdict.clone(),
        final_verdict,
        verified,
        confirmed_count,
        rejected_count,
    }
}

/// True when the kind is one the analysis treats as a safety violation.
pub fn is_violation_kind(k: FailureKind) -> bool {
    matches!(
        k,
        FailureKind::Assertion | FailureKind::DivByZero | FailureKind::Overflow
    )
}
