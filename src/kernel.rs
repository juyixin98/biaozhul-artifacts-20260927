//! Online monitoring kernel.
//!
//! A [`Monitor`] consumes one trace step at a time.  Every trigger creates an
//! independently tracked obligation; response events resolve obligations
//! according to the rule's consume policy; deadlines and the explicit end
//! marker turn pending obligations into violations.  Before a trace is
//! closed the aggregate verdict is never `satisfied` — pending is not
//! treated as a pass.
//!
//! Step application is transactional: all matching and evaluation happens in
//! a first, fallible phase; state is mutated in a second phase that cannot
//! fail, so a rejected step leaves the monitor unchanged.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::error::{AppError, AppResult};
use crate::language::{step_view, ConsumePolicy, RuleDef, Ruleset, Step, SustainScope};
use crate::matcher::{eval_predicate, pattern_matches};

/// Snapshot format version.  Bumped only when the serialized state layout
/// changes; restoring another version is refused.
pub const SNAPSHOT_FORMAT_VERSION: u32 = 1;

/// Three-valued aggregate verdict (LTL3-style).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Satisfied,
    Violated,
    Pending,
}

impl Verdict {
    pub fn as_str(self) -> &'static str {
        match self {
            Verdict::Satisfied => "satisfied",
            Verdict::Violated => "violated",
            Verdict::Pending => "pending",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObligationStatus {
    Pending,
    Satisfied,
    Violated,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObligationKind {
    Response,
    Sustain,
}

/// Serializable, observer-facing view of one obligation instance.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ObligationView {
    pub id: String,
    pub rule_id: String,
    pub kind: ObligationKind,
    pub status: ObligationStatus,
    /// Step at which the obligation was created (`None` for an
    /// `always`-scope sustain rule, which exists from step 0).
    pub trigger_step: Option<u64>,
    /// Last step on/by which the obligation can still be fulfilled.
    pub deadline_step: Option<u64>,
    /// Trigger ordinal within the rule (0-based, deterministic).
    pub ordinal: u64,
    /// Correlation key/value for response rules that declare one.
    pub correlation: Option<Correlation>,
    pub resolution_step: Option<u64>,
    pub violation_step: Option<u64>,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Correlation {
    pub key: String,
    pub value: serde_json::Value,
}

/// Resource caps enforced by the kernel / store.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Limits {
    pub max_steps: u64,
    pub max_obligations_total: usize,
    pub max_obligations_per_step: usize,
    pub max_monitors: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_steps: 1_000_000,
            max_obligations_total: 100_000,
            max_obligations_per_step: 10_000,
            max_monitors: 10_000,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResponseInstance {
    id: String,
    rule_idx: usize,
    ordinal: u64,
    trigger_step: u64,
    deadline_step: u64,
    status: ObligationStatus,
    correlation: Option<Correlation>,
    resolution_step: Option<u64>,
    violation_step: Option<u64>,
    reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SustainInstance {
    id: String,
    rule_idx: usize,
    /// Trigger ordinal; the single `always` instance uses 0.
    ordinal: u64,
    trigger_step: Option<u64>,
    deadline_step: Option<u64>,
    status: ObligationStatus,
    resolution_step: Option<u64>,
    violation_step: Option<u64>,
    reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Instance {
    Response(ResponseInstance),
    Sustain(SustainInstance),
}

impl Instance {
    fn status(&self) -> ObligationStatus {
        match self {
            Instance::Response(i) => i.status,
            Instance::Sustain(i) => i.status,
        }
    }
}

/// The online monitor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Monitor {
    pub ruleset: Ruleset,
    pub ruleset_hash: String,
    limits: Limits,
    step_count: u64,
    /// Number of applied steps that carried an observed event (a bare end
    /// marker does not count).  Used for the minimum-length check on
    /// `always`-scope sustain rules.
    event_count: u64,
    last_index: Option<u64>,
    closed: bool,
    instances: Vec<Instance>,
    /// Per-rule trigger ordinals (for deterministic ids).
    ordinals: Vec<u64>,
}

/// What changed at one applied step.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StepOutcome {
    pub index: u64,
    pub closed: bool,
    pub spawned: Vec<String>,
    pub resolved: Vec<String>,
    pub violated: Vec<String>,
    pub verdict: Verdict,
}

/// FNV-1a 64-bit content hash of the canonical rule-set JSON.
pub fn hash_ruleset(ruleset: &Ruleset) -> String {
    let bytes = ruleset.canonical_json();
    let mut hash: u64 = 0xcbf29ce484222325;
    for b in bytes {
        hash ^= b as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

impl Monitor {
    /// Construct a monitor for a validated rule set.
    pub fn new(ruleset: Ruleset, limits: Limits) -> AppResult<Self> {
        ruleset.validate()?;
        let ruleset_hash = hash_ruleset(&ruleset);
        let ordinals = vec![0u64; ruleset.rules.len()];
        let mut monitor = Self {
            ruleset_hash,
            ruleset,
            limits,
            step_count: 0,
            event_count: 0,
            last_index: None,
            closed: false,
            instances: Vec::new(),
            ordinals,
        };
        // `always`-scope sustain rules exist before the first step.
        for (idx, rule) in monitor.ruleset.rules.iter().enumerate() {
            if let RuleDef::Sustain(s) = rule {
                if matches!(s.scope, SustainScope::Always) {
                    monitor.instances.push(Instance::Sustain(SustainInstance {
                        id: format!("{}#o0", s.id),
                        rule_idx: idx,
                        ordinal: 0,
                        trigger_step: None,
                        deadline_step: None,
                        status: ObligationStatus::Pending,
                        resolution_step: None,
                        violation_step: None,
                        reason: "always_hold_pending".to_string(),
                    }));
                }
            }
        }
        Ok(monitor)
    }

    pub fn is_closed(&self) -> bool {
        self.closed
    }

    pub fn step_count(&self) -> u64 {
        self.step_count
    }

    pub fn last_index(&self) -> Option<u64> {
        self.last_index
    }

    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    /// Current aggregate verdict.
    pub fn verdict(&self) -> Verdict {
        if self.instances.iter().any(|i| i.status() == ObligationStatus::Violated) {
            Verdict::Violated
        } else if self.closed {
            Verdict::Satisfied
        } else {
            Verdict::Pending
        }
    }

    /// Observer-facing obligation instances (creation order).
    pub fn obligations(&self) -> Vec<ObligationView> {
        self.instances.iter().map(|i| self.view(i)).collect()
    }

    fn view(&self, instance: &Instance) -> ObligationView {
        let rule_id = |idx: usize| self.ruleset.rules[idx].id().to_string();
        match instance {
            Instance::Response(i) => ObligationView {
                id: i.id.clone(),
                rule_id: rule_id(i.rule_idx),
                kind: ObligationKind::Response,
                status: i.status,
                trigger_step: Some(i.trigger_step),
                deadline_step: Some(i.deadline_step),
                ordinal: i.ordinal,
                correlation: i.correlation.clone(),
                resolution_step: i.resolution_step,
                violation_step: i.violation_step,
                reason: i.reason.clone(),
            },
            Instance::Sustain(i) => ObligationView {
                id: i.id.clone(),
                rule_id: rule_id(i.rule_idx),
                kind: ObligationKind::Sustain,
                status: i.status,
                trigger_step: i.trigger_step,
                deadline_step: i.deadline_step,
                ordinal: i.ordinal,
                correlation: None,
                resolution_step: i.resolution_step,
                violation_step: i.violation_step,
                reason: i.reason.clone(),
            },
        }
    }

    /// Apply one step (or end marker).  On error the monitor is unchanged.
    pub fn apply_step(&mut self, step: &Step) -> AppResult<StepOutcome> {
        // ---- guard checks (no mutation) ----
        if self.closed {
            return Err(AppError::conflict(
                "monitor_closed",
                format!("cannot apply step {} to a closed monitor", step.index),
            ));
        }
        if let Some(v) = &step.ruleset_version {
            if v != &self.ruleset.version {
                return Err(AppError::conflict(
                    "ruleset_version_mismatch",
                    format!("step pins version `{v}`, monitor runs `{}`", self.ruleset.version),
                ));
            }
        }
        if step.index != self.step_count {
            let reason = if step.index < self.step_count {
                "step_index_duplicate_or_reordered"
            } else {
                "step_index_gap"
            };
            return Err(AppError::conflict(
                reason,
                format!("expected index {}, got {}", self.step_count, step.index),
            ));
        }
        if self.step_count >= self.limits.max_steps {
            return Err(AppError::exhausted(
                "max_steps_exceeded",
                format!("step cap {} reached", self.limits.max_steps),
            ));
        }
        let is_event = !step.event.event_type.is_empty();
        if is_event {
            step.validate()?;
        } else if !step.end {
            return Err(AppError::input(
                "empty_event_without_end",
                "a step without an event must carry `end: true`",
            ));
        }

        // ---- phase A: compute everything fallible, without mutation ----
        let mut response_matches: HashMap<usize, bool> = HashMap::new();
        let mut trigger_matches: HashMap<usize, bool> = HashMap::new();
        let mut spawn_responses: Vec<(usize, Option<Correlation>)> = Vec::new();
        let mut spawn_sustains: Vec<usize> = Vec::new();
        // Precomputed sustain evaluation results: instance idx -> holds?
        let mut sustain_eval: Vec<Option<bool>> = vec![None; self.instances.len()];
        // Condition results for sustain instances spawned this step.
        let mut new_sustain_holds: Vec<bool> = Vec::new();

        if is_event {
            for (rule_idx, rule) in self.ruleset.rules.iter().enumerate() {
                match rule {
                    RuleDef::Response(r) => {
                        let trig = pattern_matches(step, &r.trigger)?;
                        let resp = pattern_matches(step, &r.response)?;
                        trigger_matches.insert(rule_idx, trig);
                        response_matches.insert(rule_idx, resp);
                        if trig {
                            let corr = match &r.correlation_key {
                                None => None,
                                Some(key) => {
                                    let value = step_view(step).lookup(key).ok_or_else(|| {
                                        AppError::compute(
                                            "missing_correlation_fact",
                                            format!(
                                                "trigger for rule `{}` lacks correlation fact `{key}`",
                                                r.id
                                            ),
                                        )
                                    })?;
                                    Some(Correlation { key: key.clone(), value })
                                }
                            };
                            spawn_responses.push((rule_idx, corr));
                        }
                    }
                    RuleDef::Sustain(s) => {
                        match &s.scope {
                            SustainScope::Always => {}
                            SustainScope::AfterTrigger { trigger } => {
                                if pattern_matches(step, trigger)? {
                                    trigger_matches.insert(rule_idx, true);
                                    spawn_sustains.push(rule_idx);
                                    new_sustain_holds.push(eval_predicate(step, &s.condition)?);
                                }
                            }
                        }
                    }
                }
            }

            // Evaluate conditions for existing sustain windows covering n.
            for (inst_idx, instance) in self.instances.iter().enumerate() {
                if let Instance::Sustain(si) = instance {
                    if si.status != ObligationStatus::Pending {
                        continue;
                    }
                    let in_window = match (si.trigger_step, si.deadline_step) {
                        (None, None) => true, // always
                        (Some(t), Some(d)) => step.index >= t && step.index <= d,
                        (Some(_), None) | (None, Some(_)) => unreachable!(),
                    };
                    if in_window {
                        let rule = &self.ruleset.rules[si.rule_idx];
                        let cond = match rule {
                            RuleDef::Sustain(s) => &s.condition,
                            RuleDef::Response(_) => unreachable!(),
                        };
                        sustain_eval[inst_idx] = Some(eval_predicate(step, cond)?);
                    }
                }
            }
        }

        let spawn_count = spawn_responses.len() + spawn_sustains.len();
        if spawn_count > self.limits.max_obligations_per_step {
            return Err(AppError::exhausted(
                "max_obligations_per_step_exceeded",
                format!("step would spawn {spawn_count} obligations (cap {})", self.limits.max_obligations_per_step),
            ));
        }
        if self.instances.len() + spawn_count > self.limits.max_obligations_total {
            return Err(AppError::exhausted(
                "max_obligations_total_exceeded",
                format!(
                    "obligation cap {} reached ({} existing, {spawn_count} new)",
                    self.limits.max_obligations_total,
                    self.instances.len()
                ),
            ));
        }

        // ---- phase B: commit (infallible) ----
        let mut spawned = Vec::new();
        let mut resolved = Vec::new();
        let mut violated = Vec::new();
        let n = step.index;

        // B1. resolve response obligations using the event at n.
        if is_event {
            for (rule_idx, rule) in self.ruleset.rules.iter().enumerate() {
                let RuleDef::Response(rdef) = rule else { continue };
                if !response_matches.get(&rule_idx).copied().unwrap_or(false) {
                    continue;
                }
                // Pending matching instances of this rule.
                let mut candidates: Vec<usize> = Vec::new();
                for (inst_idx, instance) in self.instances.iter().enumerate() {
                    let Instance::Response(ri) = instance else { continue };
                    if ri.rule_idx != rule_idx || ri.status != ObligationStatus::Pending {
                        continue;
                    }
                    if let Some(corr) = &ri.correlation {
                        match step_view(step).lookup(&corr.key) {
                            Some(v) if v == corr.value => {}
                            _ => continue,
                        }
                    }
                    candidates.push(inst_idx);
                }
                if candidates.is_empty() {
                    continue;
                }
                let chosen: Vec<usize> = match rdef.consume {
                    ConsumePolicy::AllMatching => candidates,
                    ConsumePolicy::EarliestDeadline => {
                        let best = candidates
                            .into_iter()
                            .min_by_key(|&idx| match &self.instances[idx] {
                                Instance::Response(ri) => {
                                    (ri.deadline_step, ri.trigger_step, ri.id.clone())
                                }
                                _ => unreachable!(),
                            })
                            .expect("non-empty candidates");
                        vec![best]
                    }
                };
                for idx in chosen {
                    if let Instance::Response(ri) = &mut self.instances[idx] {
                        ri.status = ObligationStatus::Satisfied;
                        ri.resolution_step = Some(n);
                        ri.reason = "response_received".to_string();
                        resolved.push(ri.id.clone());
                    }
                }
            }
        }

        // B2. expire response deadlines.  An obligation with deadline d is
        // violated at step d once the event there did not match the
        // response; satisfaction for the equal case was handled in B1 and
        // such instances are already non-pending here.  Expired instances
        // stay violated for all later steps, so the `<= n` comparison also
        // catches instances created in earlier steps whose deadline step
        // carried no submitted event (every step here carries one event or
        // the end marker, so this fires exactly at the deadline).
        for instance in &mut self.instances {
            if let Instance::Response(ri) = instance {
                if ri.status == ObligationStatus::Pending && ri.deadline_step <= n && is_event {
                    ri.status = ObligationStatus::Violated;
                    ri.violation_step = Some(ri.deadline_step);
                    ri.reason = "deadline_expired".to_string();
                    violated.push(ri.id.clone());
                }
            }
        }

        // B3. update existing sustain windows with precomputed results.
        for (inst_idx, holds) in sustain_eval.into_iter().enumerate() {
            let Some(holds) = holds else { continue };
            let Instance::Sustain(si) = &mut self.instances[inst_idx] else { continue };
            if !holds {
                si.status = ObligationStatus::Violated;
                si.violation_step = Some(n);
                si.reason = "condition_broke".to_string();
                violated.push(si.id.clone());
            } else if si.deadline_step == Some(n) {
                si.status = ObligationStatus::Satisfied;
                si.resolution_step = Some(n);
                si.reason = "hold_window_completed".to_string();
                resolved.push(si.id.clone());
            }
        }

        // B4. spawn new obligations from triggers at n.
        for (rule_idx, correlation) in spawn_responses {
            let ordinal = self.ordinals[rule_idx];
            self.ordinals[rule_idx] += 1;
            let id = format!("{}#o{ordinal}", self.ruleset.rules[rule_idx].id());
            let within = match &self.ruleset.rules[rule_idx] {
                RuleDef::Response(r) => r.within_steps,
                _ => unreachable!(),
            };
            spawned.push(id.clone());
            self.instances.push(Instance::Response(ResponseInstance {
                id,
                rule_idx,
                ordinal,
                trigger_step: n,
                deadline_step: n + within,
                status: ObligationStatus::Pending,
                correlation,
                resolution_step: None,
                violation_step: None,
                reason: "awaiting_response".to_string(),
            }));
        }
        for (k, rule_idx) in spawn_sustains.into_iter().enumerate() {
            let ordinal = self.ordinals[rule_idx];
            self.ordinals[rule_idx] += 1;
            let duration = match &self.ruleset.rules[rule_idx] {
                RuleDef::Sustain(s) => s.duration_steps,
                _ => unreachable!(),
            };
            let id = format!("{}#o{ordinal}", self.ruleset.rules[rule_idx].id());
            let holds = new_sustain_holds[k];
            spawned.push(id.clone());
            let status;
            let reason;
            let mut violation_step = None;
            let mut resolution_step = None;
            if !holds {
                status = ObligationStatus::Violated;
                violation_step = Some(n);
                reason = "condition_broke";
                violated.push(id.clone());
            } else if duration == 1 {
                status = ObligationStatus::Satisfied;
                resolution_step = Some(n);
                reason = "hold_window_completed";
                resolved.push(id.clone());
            } else {
                status = ObligationStatus::Pending;
                reason = "hold_in_progress";
            }
            self.instances.push(Instance::Sustain(SustainInstance {
                id,
                rule_idx,
                ordinal,
                trigger_step: Some(n),
                deadline_step: Some(n + duration - 1),
                status,
                resolution_step,
                violation_step,
                reason: reason.to_string(),
            }));
        }

        self.step_count += 1;
        if is_event {
            self.event_count += 1;
        }
        self.last_index = Some(n);

        // B5. end marker closes the trace: remaining pending obligations are
        // sealed.
        if step.end {
            self.closed = true;
            for instance in &mut self.instances {
                match instance {
                    Instance::Response(ri) if ri.status == ObligationStatus::Pending => {
                        ri.status = ObligationStatus::Violated;
                        ri.violation_step = Some(n);
                        ri.reason = "trace_closed_unresolved".to_string();
                        violated.push(ri.id.clone());
                    }
                    Instance::Sustain(si) if si.status == ObligationStatus::Pending => {
                        // `always` held on every observed step; triggered
                        // windows that did not complete are violations.
                        if si.trigger_step.is_none() {
                            let duration = match &self.ruleset.rules[si.rule_idx] {
                                RuleDef::Sustain(s) => s.duration_steps,
                                RuleDef::Response(_) => unreachable!(),
                            };
                            if self.event_count == 0 {
                                // Empty trace: universal condition holds
                                // vacuously.
                                si.status = ObligationStatus::Satisfied;
                                si.resolution_step = Some(n);
                                si.reason = "vacuously_held_empty_trace".to_string();
                                resolved.push(si.id.clone());
                            } else if self.event_count >= duration {
                                si.status = ObligationStatus::Satisfied;
                                si.resolution_step = Some(n);
                                si.reason = "held_throughout".to_string();
                                resolved.push(si.id.clone());
                            } else {
                                si.status = ObligationStatus::Violated;
                                si.violation_step = Some(n);
                                si.reason = "trace_too_short_for_duration".to_string();
                                violated.push(si.id.clone());
                            }
                        } else {
                            si.status = ObligationStatus::Violated;
                            si.violation_step = Some(n);
                            si.reason = "trace_closed_incomplete".to_string();
                            violated.push(si.id.clone());
                        }
                    }
                    _ => {}
                }
            }
        }

        Ok(StepOutcome {
            index: n,
            closed: self.closed,
            spawned,
            resolved,
            violated,
            verdict: self.verdict(),
        })
    }

    /// Serialize monitor state (paired with [`Monitor::restore`]).
    pub fn snapshot(&self) -> SavedMonitor {
        SavedMonitor {
            snapshot_format: SNAPSHOT_FORMAT_VERSION,
            ruleset_id: self.ruleset.id.clone(),
            ruleset_version: self.ruleset.version.clone(),
            ruleset_hash: self.ruleset_hash.clone(),
            step_count: self.step_count,
            event_count: self.event_count,
            last_index: self.last_index,
            closed: self.closed,
            limits: self.limits.clone(),
            ordinals: self.ordinals.clone(),
            instances: self.instances.clone(),
        }
    }

    /// Rebuild a monitor from a snapshot against the *current* rule set.
    /// Different rule-set version or content is a state conflict: old rule
    /// state is never mixed with new rules.
    pub fn restore(snapshot: SavedMonitor, ruleset: &Ruleset) -> AppResult<Self> {
        ruleset.validate()?;
        if snapshot.snapshot_format != SNAPSHOT_FORMAT_VERSION {
            return Err(AppError::conflict(
                "snapshot_format_unsupported",
                format!(
                    "snapshot format {} supported is {SNAPSHOT_FORMAT_VERSION}",
                    snapshot.snapshot_format
                ),
            ));
        }
        if snapshot.ruleset_id != ruleset.id {
            return Err(AppError::conflict(
                "ruleset_id_mismatch",
                format!("snapshot belongs to ruleset `{}`, got `{}`", snapshot.ruleset_id, ruleset.id),
            ));
        }
        if snapshot.ruleset_version != ruleset.version {
            return Err(AppError::conflict(
                "ruleset_version_mismatch",
                format!(
                    "snapshot produced under ruleset version `{}`, current is `{}`",
                    snapshot.ruleset_version, ruleset.version
                ),
            ));
        }
        let current_hash = hash_ruleset(ruleset);
        if snapshot.ruleset_hash != current_hash {
            return Err(AppError::conflict(
                "ruleset_content_mismatch",
                format!(
                    "ruleset `{}` version `{}` content differs from the one that produced the snapshot",
                    ruleset.id, ruleset.version
                ),
            ));
        }
        if snapshot.ordinals.len() != ruleset.rules.len() {
            return Err(AppError::conflict(
                "snapshot_corrupt",
                "snapshot ordinal table does not match rule count",
            ));
        }
        if snapshot.last_index.map(|i| i + 1).unwrap_or(0) != snapshot.step_count {
            return Err(AppError::conflict(
                "snapshot_corrupt",
                "snapshot step_count/last_index inconsistent",
            ));
        }
        // All rule references inside instances must resolve; cheap integrity
        // check against tampered snapshots.
        for instance in &snapshot.instances {
            let idx = match instance {
                Instance::Response(i) => i.rule_idx,
                Instance::Sustain(i) => i.rule_idx,
            };
            if idx >= ruleset.rules.len() {
                return Err(AppError::conflict("snapshot_corrupt", "instance references unknown rule"));
            }
        }
        Ok(Self {
            ruleset: ruleset.clone(),
            ruleset_hash: current_hash,
            limits: snapshot.limits,
            step_count: snapshot.step_count,
            event_count: snapshot.event_count,
            last_index: snapshot.last_index,
            closed: snapshot.closed,
            instances: snapshot.instances,
            ordinals: snapshot.ordinals,
        })
    }
}

/// Persisted monitor state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SavedMonitor {
    pub snapshot_format: u32,
    pub ruleset_id: String,
    pub ruleset_version: String,
    pub ruleset_hash: String,
    pub step_count: u64,
    pub event_count: u64,
    pub last_index: Option<u64>,
    pub closed: bool,
    pub limits: Limits,
    pub ordinals: Vec<u64>,
    pub instances: Vec<Instance>,
}
