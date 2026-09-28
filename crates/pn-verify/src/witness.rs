//! Independent replay of a claimed firing sequence.

use pn_core::fire::{fire, fire_failure_name};
use pn_core::{Marking, Net, Token};

/// One claimed step: a named transition and (optionally) the marking the
/// claimant expects afterwards.
#[derive(Debug, Clone)]
pub struct StepClaim {
    pub transition: String,
    /// If present, the replay must produce exactly this marking.
    pub expected_after: Option<Vec<Token>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VerifyFailureKind {
    UnknownTransition,
    FiringIllegal,
    ClaimedMarkingMismatch,
    EndpointMismatch,
    BadLength,
}

impl VerifyFailureKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            VerifyFailureKind::UnknownTransition => "UNKNOWN_TRANSITION",
            VerifyFailureKind::FiringIllegal => "FIRING_ILLEGAL",
            VerifyFailureKind::ClaimedMarkingMismatch => "CLAIMED_MARKING_MISMATCH",
            VerifyFailureKind::EndpointMismatch => "ENDPOINT_MISMATCH",
            VerifyFailureKind::BadLength => "BAD_LENGTH",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifyFailure {
    pub kind: VerifyFailureKind,
    /// 1-based step number, or 0 for endpoint-level failures.
    pub step: usize,
    pub detail: String,
    /// Kernel failure category when the firing itself was illegal.
    pub fire_failure: Option<&'static str>,
}

#[derive(Debug, Clone)]
pub struct VerifiedStep {
    pub step: usize,
    pub transition: String,
    pub before: Marking,
    pub after: Marking,
}

#[derive(Debug, Clone)]
pub struct VerifiedWitness {
    pub valid: bool,
    pub endpoint: Marking,
    pub steps: Vec<VerifiedStep>,
    pub failures: Vec<VerifyFailure>,
}

/// Replay `claims` from the net's initial marking. When `expected_end` is
/// given, the final marking must equal it. Verification never short-circuits
/// silently: the first illegal firing stops replay (no successor exists), but
/// claim mismatches at every reachable step are collected first.
pub fn verify_witness(
    net: &Net,
    claims: &[StepClaim],
    expected_end: Option<&[Token]>,
) -> VerifiedWitness {
    let mut failures: Vec<VerifyFailure> = Vec::new();
    let mut current: Marking = net.initial().to_vec();
    let mut steps = Vec::with_capacity(claims.len());

    for (i, claim) in claims.iter().enumerate() {
        let step_no = i + 1;
        let Some(t) = net.transition_index(&claim.transition) else {
            failures.push(VerifyFailure {
                kind: VerifyFailureKind::UnknownTransition,
                step: step_no,
                detail: format!("transition '{}' is not defined", claim.transition),
                fire_failure: None,
            });
            break; // cannot continue without a real transition
        };

        match fire(net, &current, t) {
            Ok(next) => {
                if let Some(expected) = &claim.expected_after {
                    if expected.len() != next.len() {
                        failures.push(VerifyFailure {
                            kind: VerifyFailureKind::BadLength,
                            step: step_no,
                            detail: format!(
                                "claimed after-marking has {} entries, net has {}",
                                expected.len(),
                                next.len()
                            ),
                            fire_failure: None,
                        });
                    } else if expected != &next {
                        failures.push(VerifyFailure {
                            kind: VerifyFailureKind::ClaimedMarkingMismatch,
                            step: step_no,
                            detail: format!(
                                "after firing '{}' the marking is {next:?}, not {expected:?}",
                                claim.transition
                            ),
                            fire_failure: None,
                        });
                    }
                }
                steps.push(VerifiedStep {
                    step: step_no,
                    transition: claim.transition.clone(),
                    before: current.clone(),
                    after: next.clone(),
                });
                current = next;
            }
            Err(fe) => {
                failures.push(VerifyFailure {
                    kind: VerifyFailureKind::FiringIllegal,
                    step: step_no,
                    detail: format!(
                        "firing '{}' from {current:?} is illegal: {fe}",
                        claim.transition
                    ),
                    fire_failure: Some(fire_failure_name(&fe)),
                });
                break; // no successor marking exists
            }
        }
    }

    if let Some(end) = expected_end {
        if end.len() != current.len() {
            failures.push(VerifyFailure {
                kind: VerifyFailureKind::BadLength,
                step: 0,
                detail: format!(
                    "expected endpoint has {} entries, net has {}",
                    end.len(),
                    current.len()
                ),
                fire_failure: None,
            });
        } else if end != current {
            failures.push(VerifyFailure {
                kind: VerifyFailureKind::EndpointMismatch,
                step: 0,
                detail: format!("witness ends at {current:?}, expected {end:?}"),
                fire_failure: None,
            });
        }
    }

    VerifiedWitness {
        valid: failures.is_empty(),
        endpoint: current,
        steps,
        failures,
    }
}
