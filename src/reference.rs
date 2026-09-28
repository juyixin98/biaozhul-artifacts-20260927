//! Independent reference evaluator.
//!
//! This is the **oracle**: a deliberately naive, set-based re-evaluation that
//! shares only the input-language types with the incremental kernel. Given
//! the whole trace up front it enumerates *every* possible obligation (one
//! per trigger event), then decides each by scanning the trace. The kernel
//! (`crate::monitor`) instead maintains pending state incrementally; nothing
//! in this file reads kernel state or its decision log, so agreement between
//! the two is genuine cross-validation rather than self-congratulation.
//!
//! Epoch rotation is modelled as a list of segments: each segment is one
//! ruleset applied to a contiguous slice of the trace, and obligations open
//! at a segment boundary are sealed with that rule's close policy — exactly
//! what the kernel's `rotate` does.

use std::collections::BTreeMap;

use serde::Serialize;

use crate::lang::*;

/// One evaluated obligation instance, keyed the same way the kernel keys it
/// (`"{epoch}:{rule}:{trigger_step}"`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RefInstance {
    pub epoch: usize,
    pub rule_id: String,
    pub kind: &'static str,
    pub trigger_step: i64,
    pub window_start: i64,
    pub window_end: i64,
    pub status: ObligationStatus,
    pub reason: ObligationReason,
    pub satisfied_at: Option<i64>,
    pub failed_at: Option<i64>,
}

type InstId = String;

/// Evaluate one ruleset over a closed or open segment trace.
///
/// `base` is the absolute step index of `trace[0]` (used only for sealing at
/// an empty boundary). Steps are absolute across the whole run — rotation
/// seals old obligations and tags new ones with the epoch, but the trace
/// timeline is not rewound.
/// `closed=false` leaves not-yet-decidable instances `wait`.
pub fn evaluate(
    trace: &[(i64, Event)],
    base: i64,
    epoch: usize,
    rs: &RuleSet,
    closed: bool,
) -> BTreeMap<InstId, RefInstance> {
    let mut out = BTreeMap::new();
    let last_abs = if trace.is_empty() {
        base - 1
    } else {
        trace[trace.len() - 1].0
    };
    let event_at =
        |s: i64| -> Option<&Event> { trace.iter().find_map(|(t, ev)| (*t == s).then_some(ev)) };
    let make_id = |rule: &str, t: i64| format!("{}:{}:{}", epoch, rule, t);

    // ========================= response rules ===========================
    for r in &rs.response {
        // Enumerate every firing (one trigger event = one obligation).
        let mut fired: Vec<(i64, i64, i64)> = Vec::new(); // (trigger, ws, we)
        for (t, ev) in trace {
            if r.trigger.matches(ev) {
                let ws = t + r.after;
                let we = ws + r.within - 1;
                fired.push((*t, ws, we));
            }
        }

        // All response events with their steps (existential scan).
        let responses: Vec<i64> = trace
            .iter()
            .filter_map(|(t, ev)| r.response.matches(ev).then_some(*t))
            .collect();

        match r.satisfy {
            SatisfyMode::All => {
                // Each instance independently checks for any response in its
                // window. The same response step may satisfy many instances.
                for (t, ws, we) in &fired {
                    let hit = responses
                        .iter()
                        .copied()
                        .filter(|s| *s >= *ws && *s <= *we)
                        .min();
                    let id = make_id(&r.id, *t);
                    out.insert(
                        id,
                        decide_response(r, epoch, (*t, *ws, *we), hit, closed, last_abs),
                    );
                }
            }
            SatisfyMode::One => {
                // Earliest-eligible greedy matching: sort instances by window
                // start then trigger, and consume each matched response step
                // at most once.
                let mut order = fired;
                order.sort_by_key(|(t, ws, _we)| (*ws, *t));
                let mut used: Vec<i64> = Vec::new();
                for (t, ws, we) in order {
                    let hit = responses
                        .iter()
                        .copied()
                        .find(|s| !used.contains(s) && *s >= ws && *s <= we);
                    if let Some(s) = hit {
                        used.push(s);
                        let id = make_id(&r.id, t);
                        out.insert(
                            id,
                            RefInstance {
                                epoch,
                                rule_id: r.id.clone(),
                                kind: "response",
                                trigger_step: t,
                                window_start: ws,
                                window_end: we,
                                status: ObligationStatus::Satisfied,
                                reason: ObligationReason::Fulfilled,
                                satisfied_at: Some(s),
                                failed_at: None,
                            },
                        );
                    } else {
                        let id = make_id(&r.id, t);
                        out.insert(
                            id,
                            decide_response(r, epoch, (t, ws, we), None, closed, last_abs),
                        );
                    }
                }
            }
        }
    }

    // ========================== sustain rules ===========================
    for r in &rs.sustain {
        for (t, ev) in trace {
            if !r.trigger.matches(ev) {
                continue;
            }
            let ws = t + r.after;
            let we = ws + r.duration - 1;
            let id = make_id(&r.id, *t);

            // Universal quantification over every window step with an event.
            let mut failure: Option<i64> = None;
            let mut observed_all = true;
            for s in ws..=we {
                match event_at(s) {
                    Some(sev) if r.sustain.matches(sev) => {}
                    Some(_) => {
                        failure = Some(s);
                        break;
                    }
                    None => {
                        observed_all = false;
                        break;
                    }
                }
            }

            let inst = if let Some(at) = failure {
                RefInstance {
                    epoch,
                    rule_id: r.id.clone(),
                    kind: "sustain",
                    trigger_step: *t,
                    window_start: ws,
                    window_end: we,
                    status: ObligationStatus::Violated,
                    reason: ObligationReason::ConditionFailed,
                    satisfied_at: None,
                    failed_at: Some(at),
                }
            } else if observed_all {
                RefInstance {
                    epoch,
                    rule_id: r.id.clone(),
                    kind: "sustain",
                    trigger_step: *t,
                    window_start: ws,
                    window_end: we,
                    status: ObligationStatus::Satisfied,
                    reason: ObligationReason::Fulfilled,
                    satisfied_at: Some(we),
                    failed_at: None,
                }
            } else if closed {
                seal_premature(r, epoch, *t, ws, we, last_abs)
            } else {
                RefInstance {
                    epoch,
                    rule_id: r.id.clone(),
                    kind: "sustain",
                    trigger_step: *t,
                    window_start: ws,
                    window_end: we,
                    status: ObligationStatus::Pending,
                    reason: ObligationReason::Pending,
                    satisfied_at: None,
                    failed_at: None,
                }
            };
            out.insert(id, inst);
        }
    }

    out
}

/// `(trigger_step, window_start, window_end)` of one response instance.
type RespWindow = (i64, i64, i64);

fn decide_response(
    r: &ResponseRule,
    epoch: usize,
    (t, ws, we): RespWindow,
    hit: Option<i64>,
    closed: bool,
    last_abs: i64,
) -> RefInstance {
    if let Some(at) = hit {
        RefInstance {
            epoch,
            rule_id: r.id.clone(),
            kind: "response",
            trigger_step: t,
            window_start: ws,
            window_end: we,
            status: ObligationStatus::Satisfied,
            reason: ObligationReason::Fulfilled,
            satisfied_at: Some(at),
            failed_at: None,
        }
    } else if last_abs >= we {
        // Deadline fully elapsed on an observed step: a genuine miss.
        RefInstance {
            epoch,
            rule_id: r.id.clone(),
            kind: "response",
            trigger_step: t,
            window_start: ws,
            window_end: we,
            status: ObligationStatus::Violated,
            reason: ObligationReason::DeadlineMissed,
            satisfied_at: None,
            failed_at: Some(we),
        }
    } else if closed {
        // Closed before the deadline could be reached.
        match r.on_close {
            ClosePolicy::Strict => RefInstance {
                epoch,
                rule_id: r.id.clone(),
                kind: "response",
                trigger_step: t,
                window_start: ws,
                window_end: we,
                status: ObligationStatus::Violated,
                reason: ObligationReason::ClosedPending,
                satisfied_at: None,
                failed_at: Some(last_abs),
            },
            ClosePolicy::Lenient => RefInstance {
                epoch,
                rule_id: r.id.clone(),
                kind: "response",
                trigger_step: t,
                window_start: ws,
                window_end: we,
                status: ObligationStatus::Satisfied,
                reason: ObligationReason::ClosedAccepted,
                satisfied_at: Some(last_abs),
                failed_at: None,
            },
        }
    } else {
        RefInstance {
            epoch,
            rule_id: r.id.clone(),
            kind: "response",
            trigger_step: t,
            window_start: ws,
            window_end: we,
            status: ObligationStatus::Pending,
            reason: ObligationReason::Pending,
            satisfied_at: None,
            failed_at: None,
        }
    }
}

fn seal_premature(
    r: &SustainRule,
    epoch: usize,
    t: i64,
    ws: i64,
    we: i64,
    last_abs: i64,
) -> RefInstance {
    match r.on_close {
        ClosePolicy::Strict => RefInstance {
            epoch,
            rule_id: r.id.clone(),
            kind: "sustain",
            trigger_step: t,
            window_start: ws,
            window_end: we,
            status: ObligationStatus::Violated,
            reason: ObligationReason::ClosedIncomplete,
            satisfied_at: None,
            failed_at: Some(last_abs),
        },
        ClosePolicy::Lenient => RefInstance {
            epoch,
            rule_id: r.id.clone(),
            kind: "sustain",
            trigger_step: t,
            window_start: ws,
            window_end: we,
            status: ObligationStatus::Satisfied,
            reason: ObligationReason::ClosedAccepted,
            satisfied_at: Some(last_abs),
            failed_at: None,
        },
    }
}

/// Replay a whole run with rotation segments through the oracle.
///
/// `segments[i] = (ruleset, trace slice)` with absolute trace steps. Every
/// segment except the last is implicitly closed at its boundary; the last is
/// closed iff `closed`.
pub fn evaluate_run(
    segments: &[(RuleSet, Vec<(i64, Event)>)],
    closed: bool,
) -> BTreeMap<InstId, RefInstance> {
    let mut all = BTreeMap::new();
    for (epoch, (rs, trace)) in segments.iter().enumerate() {
        let base = trace.first().map(|(t, _)| *t).unwrap_or(0);
        let seg_closed = closed || epoch + 1 < segments.len();
        all.extend(evaluate(trace, base, epoch, rs, seg_closed));
    }
    all
}

/// Global three-valued verdict of an oracle result map.
pub fn global_verdict(map: &BTreeMap<InstId, RefInstance>) -> Verdict {
    map.values()
        .map(|i| match i.status {
            ObligationStatus::Satisfied => Verdict::Sat,
            ObligationStatus::Violated => Verdict::Viol,
            ObligationStatus::Pending => Verdict::Wait,
        })
        .fold(Verdict::Sat, Verdict::merge)
}
