//! Independent offline oracle ("unfold" evaluation).
//!
//! Given the complete trace up front, this module replays it with its own
//! obligation bookkeeping.  It deliberately shares with the online kernel
//! only the language types (`language.rs`) and the predicate primitive
//! (`matcher.rs`); none of the kernel's state-machine code is used.  Tests
//! cross-check the kernel against this second implementation and against
//! hand-computed fixture expectations, so the reference answer is never
//! produced solely by the code under test.

use crate::error::{AppError, AppResult};
use crate::kernel::{Correlation, ObligationKind, ObligationStatus, ObligationView, Verdict};
use crate::language::{ConsumePolicy, RuleDef, Ruleset, Step, SustainScope};
use crate::matcher::{eval_predicate, pattern_matches};

/// One obligation as reconstructed by the offline replay.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct OracleInstance {
    pub id: String,
    pub rule_id: String,
    pub kind: ObligationKind,
    pub status: ObligationStatus,
    pub trigger_step: Option<u64>,
    pub deadline_step: Option<u64>,
    pub ordinal: u64,
    pub correlation: Option<Correlation>,
    pub resolution_step: Option<u64>,
    pub violation_step: Option<u64>,
    pub reason: String,
}

#[derive(Debug, Clone)]
struct RespRec {
    ordinal: u64,
    trigger_step: u64,
    deadline: u64,
    correlation: Option<Correlation>,
    status: ObligationStatus,
    resolution_step: Option<u64>,
    violation_step: Option<u64>,
    closed_pending: bool,
}

#[derive(Debug, Clone)]
struct SusRec {
    ordinal: u64,
    trigger_step: Option<u64>,
    deadline: Option<u64>,
    status: ObligationStatus,
    resolution_step: Option<u64>,
    violation_step: Option<u64>,
    closed_pending: bool,
}

/// Result of an offline evaluation.
#[derive(Debug, Clone)]
pub struct OracleReport {
    pub verdict: Verdict,
    pub closed: bool,
    pub obligations: Vec<OracleInstance>,
}

/// Categorical fields shared when comparing an oracle instance with a
/// kernel obligation view (human-readable `reason` deliberately excluded).
pub type ObligationKey = (
    String,
    ObligationKind,
    ObligationStatus,
    Option<u64>,
    Option<u64>,
    u64,
    Option<Correlation>,
    Option<u64>,
    Option<u64>,
);

impl OracleInstance {
    /// Fields used when comparing oracle and kernel.  The human-readable
    /// `reason` is intentionally excluded: the two implementations choose
    /// their own phrasing, and the test asserts the categorical outcome.
    pub fn comparison_key(&self) -> ObligationKey {
        (
            self.id.clone(),
            self.kind,
            self.status,
            self.trigger_step,
            self.deadline_step,
            self.ordinal,
            self.correlation.clone(),
            self.resolution_step,
            self.violation_step,
        )
    }
}

/// Convert a kernel DTO into the same comparison tuple.
pub fn view_key(v: &ObligationView) -> ObligationKey {
    (
        v.id.clone(),
        v.kind,
        v.status,
        v.trigger_step,
        v.deadline_step,
        v.ordinal,
        v.correlation.clone(),
        v.resolution_step,
        v.violation_step,
    )
}

/// Independently validate step ordering, the way the kernel does.
pub fn check_trace_shape(steps: &[Step]) -> AppResult<()> {
    for (i, step) in steps.iter().enumerate() {
        if step.index as usize != i {
            return Err(AppError::input(
                "non_contiguous_trace",
                format!("step {i} declares index {}", step.index),
            ));
        }
    }
    if let Some(pos) = steps.iter().position(|s| s.end) {
        if pos != steps.len() - 1 {
            return Err(AppError::input("end_not_last", "an end marker must be the last step"));
        }
    }
    Ok(())
}

/// Replay a whole trace from scratch.
pub fn evaluate(ruleset: &Ruleset, steps: &[Step]) -> AppResult<OracleReport> {
    ruleset.validate()?;
    check_trace_shape(steps)?;

    let rules = &ruleset.rules;
    // Per-rule scratch state.
    let mut resp: Vec<Vec<RespRec>> = vec![Vec::new(); rules.len()];
    let mut sus: Vec<Vec<SusRec>> = vec![Vec::new(); rules.len()];
    let mut ordinals = vec![0u64; rules.len()];

    // The single `always` window per always-sustain rule.
    let mut always: Vec<Option<(bool, Option<u64>)>> = vec![None; rules.len()];
    for (idx, rule) in rules.iter().enumerate() {
        if let RuleDef::Sustain(s) = rule {
            if matches!(s.scope, SustainScope::Always) {
                always[idx] = Some((true, None)); // (still holding, violation step)
            }
        }
    }

    let mut closed = false;
    let mut event_count = 0u64;

    for (i, step) in steps.iter().enumerate() {
        let n = i as u64;
        let is_event = !step.event.event_type.is_empty();
        if !is_event && !step.end {
            return Err(AppError::input(
                "empty_event_without_end",
                "a step without an event must carry `end: true`",
            ));
        }
        if is_event {
            event_count += 1;
        }

        if is_event {
            for (rule_idx, rule) in rules.iter().enumerate() {
                match rule {
                    RuleDef::Response(rdef) => {
                        // 1) belated expiration (deadline strictly before n)
                        for rec in resp[rule_idx].iter_mut() {
                            if rec.status == ObligationStatus::Pending && rec.deadline < n {
                                rec.status = ObligationStatus::Violated;
                                rec.violation_step = Some(rec.deadline);
                            }
                        }
                        // 2) resolve with the event at n
                        if pattern_matches(step, &rdef.response)? {
                            let candidates: Vec<usize> = resp[rule_idx]
                                .iter()
                                .enumerate()
                                .filter(|(_, rec)| {
                                    rec.status == ObligationStatus::Pending
                                        && rec.deadline >= n
                                        && match (&rec.correlation, &rdef.correlation_key) {
                                            (None, _) => true,
                                            (Some(c), _) => crate::language::step_view(step)
                                                .lookup(&c.key)
                                                .map(|v| v == c.value)
                                                .unwrap_or(false),
                                        }
                                })
                                .map(|(idx, _)| idx)
                                .collect();
                            if !candidates.is_empty() {
                                let pick: Vec<usize> = match rdef.consume {
                                    ConsumePolicy::AllMatching => candidates,
                                    ConsumePolicy::EarliestDeadline => {
                                        let best = candidates
                                            .into_iter()
                                            .min_by_key(|&idx| {
                                                let rec = &resp[rule_idx][idx];
                                                (rec.deadline, rec.trigger_step, rec.ordinal)
                                            })
                                            .unwrap();
                                        vec![best]
                                    }
                                };
                                for idx in pick {
                                    resp[rule_idx][idx].status = ObligationStatus::Satisfied;
                                    resp[rule_idx][idx].resolution_step = Some(n);
                                }
                            }
                        }
                        // 3) expiration of obligations whose deadline is n
                        for rec in resp[rule_idx].iter_mut() {
                            if rec.status == ObligationStatus::Pending && rec.deadline <= n {
                                rec.status = ObligationStatus::Violated;
                                rec.violation_step = Some(n);
                            }
                        }
                        // 4) new trigger at n (cannot be satisfied at n)
                        if pattern_matches(step, &rdef.trigger)? {
                            let correlation = match &rdef.correlation_key {
                                None => None,
                                Some(key) => Some(Correlation {
                                    key: key.clone(),
                                    value: crate::language::step_view(step)
                                        .lookup(key)
                                        .ok_or_else(|| {
                                            AppError::compute(
                                                "missing_correlation_fact",
                                                format!(
                                                    "trigger for rule `{}` lacks `{key}`",
                                                    rdef.id
                                                ),
                                            )
                                        })?,
                                }),
                            };
                            let ordinal = ordinals[rule_idx];
                            ordinals[rule_idx] += 1;
                            resp[rule_idx].push(RespRec {
                                ordinal,
                                trigger_step: n,
                                deadline: n + rdef.within_steps,
                                correlation,
                                status: ObligationStatus::Pending,
                                resolution_step: None,
                                violation_step: None,
                                closed_pending: false,
                            });
                        }
                    }
                    RuleDef::Sustain(sdef) => match &sdef.scope {
                        SustainScope::Always => {
                            let holds = eval_predicate(step, &sdef.condition)?;
                            if let Some(state @ (true, None)) = &mut always[rule_idx] {
                                if !holds {
                                    *state = (false, Some(n));
                                }
                            }
                        }
                        SustainScope::AfterTrigger { trigger } => {
                            // Evaluate open windows at n.
                            for rec in sus[rule_idx].iter_mut() {
                                if rec.status != ObligationStatus::Pending {
                                    continue;
                                }
                                let t = rec.trigger_step.unwrap();
                                let d = rec.deadline.unwrap();
                                if n >= t && n <= d {
                                    let holds = eval_predicate(step, &sdef.condition)?;
                                    if !holds {
                                        rec.status = ObligationStatus::Violated;
                                        rec.violation_step = Some(n);
                                    } else if d == n {
                                        rec.status = ObligationStatus::Satisfied;
                                        rec.resolution_step = Some(n);
                                    }
                                }
                            }
                            // New trigger window starting at n.
                            if pattern_matches(step, trigger)? {
                                let ordinal = ordinals[rule_idx];
                                ordinals[rule_idx] += 1;
                                let holds = eval_predicate(step, &sdef.condition)?;
                                let deadline = n + sdef.duration_steps - 1;
                                let (status, resolution_step, violation_step) = if !holds {
                                    (ObligationStatus::Violated, None, Some(n))
                                } else if deadline == n {
                                    (ObligationStatus::Satisfied, Some(n), None)
                                } else {
                                    (ObligationStatus::Pending, None, None)
                                };
                                sus[rule_idx].push(SusRec {
                                    ordinal,
                                    trigger_step: Some(n),
                                    deadline: Some(deadline),
                                    status,
                                    resolution_step,
                                    violation_step,
                                    closed_pending: false,
                                });
                            }
                        }
                    },
                }
            }
        }

        if step.end {
            closed = true;
            for (rule_idx, rule) in rules.iter().enumerate() {
                let RuleDef::Response(_) = rule else { continue };
                for rec in resp[rule_idx].iter_mut() {
                    if rec.status == ObligationStatus::Pending {
                        rec.status = ObligationStatus::Violated;
                        rec.violation_step = Some(n);
                        rec.closed_pending = true;
                    }
                }
                if let RuleDef::Sustain(_) = rule {
                    for rec in sus[rule_idx].iter_mut() {
                        if rec.status == ObligationStatus::Pending {
                            rec.status = ObligationStatus::Violated;
                            rec.violation_step = Some(n);
                            rec.closed_pending = true;
                        }
                    }
                }
            }
        }
    }

    // ---- assemble result in creation order ----
    let mut obligations = Vec::new();
    for (rule_idx, rule) in rules.iter().enumerate() {
        if let Some((holds, broke_at)) = always[rule_idx] {
            let duration = match rule {
                RuleDef::Sustain(s) => s.duration_steps,
                RuleDef::Response(_) => unreachable!(),
            };
            let (status, violation_step, reason) = if !holds {
                (ObligationStatus::Violated, broke_at, "condition_broke".to_string())
            } else if closed && event_count == 0 {
                (ObligationStatus::Satisfied, None, "vacuously_held_empty_trace".to_string())
            } else if closed && event_count >= duration {
                (ObligationStatus::Satisfied, None, "held_throughout".to_string())
            } else if closed {
                (
                    ObligationStatus::Violated,
                    steps.last().map(|s| s.index),
                    "trace_too_short_for_duration".to_string(),
                )
            } else {
                (ObligationStatus::Pending, None, "always_hold_pending".to_string())
            };
            obligations.push(OracleInstance {
                id: format!("{}#o0", rule.id()),
                rule_id: rule.id().to_string(),
                kind: ObligationKind::Sustain,
                status,
                trigger_step: None,
                deadline_step: None,
                ordinal: 0,
                correlation: None,
                resolution_step: if status == ObligationStatus::Satisfied {
                    steps.last().filter(|s| s.end).map(|s| s.index)
                } else {
                    None
                },
                violation_step,
                reason,
            });
        }
        for rec in &resp[rule_idx] {
            obligations.push(OracleInstance {
                id: format!("{}#o{}", rule.id(), rec.ordinal),
                rule_id: rule.id().to_string(),
                kind: ObligationKind::Response,
                status: rec.status,
                trigger_step: Some(rec.trigger_step),
                deadline_step: Some(rec.deadline),
                ordinal: rec.ordinal,
                correlation: rec.correlation.clone(),
                resolution_step: rec.resolution_step,
                violation_step: rec.violation_step,
                reason: match rec.status {
                    ObligationStatus::Satisfied => "response_received".to_string(),
                    ObligationStatus::Violated => {
                        if rec.closed_pending {
                            "trace_closed_unresolved".to_string()
                        } else {
                            "deadline_expired".to_string()
                        }
                    }
                    ObligationStatus::Pending => "awaiting_response".to_string(),
                },
            });
        }
        for rec in &sus[rule_idx] {
            obligations.push(OracleInstance {
                id: format!("{}#o{}", rule.id(), rec.ordinal),
                rule_id: rule.id().to_string(),
                kind: ObligationKind::Sustain,
                status: rec.status,
                trigger_step: rec.trigger_step,
                deadline_step: rec.deadline,
                ordinal: rec.ordinal,
                correlation: None,
                resolution_step: rec.resolution_step,
                violation_step: rec.violation_step,
                reason: match rec.status {
                    ObligationStatus::Satisfied => "hold_window_completed".to_string(),
                    ObligationStatus::Violated => {
                        if rec.closed_pending {
                            "trace_closed_incomplete".to_string()
                        } else {
                            "condition_broke".to_string()
                        }
                    }
                    ObligationStatus::Pending => "hold_in_progress".to_string(),
                },
            });
        }
    }

    let verdict = if obligations.iter().any(|o| o.status == ObligationStatus::Violated) {
        Verdict::Violated
    } else if closed {
        Verdict::Satisfied
    } else {
        Verdict::Pending
    };

    Ok(OracleReport { verdict, closed, obligations })
}
