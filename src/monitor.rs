//! Incremental monitoring kernel.
//!
//! One [`Monitor`] consumes a finite trace step by step. Each trigger creates
//! an independent [`Obligation`] tagged with the epoch (ruleset version) in
//! which it was born. Events are processed in five ordered micro-phases per
//! step: spawn → respond → deadline-sweep → sustain-check → sustain-complete.
//! All state transitions append a hash-chained [`DecisionEntry`], which makes
//! the run replayable and tamper-evident.

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::canonical::{canonical_bytes, canonical_digest, sha256_hex};
use crate::error::{KernelError, Result};
use crate::lang::*;

/// Obligation flavour (mirrors the two rule kinds).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Response,
    Sustain,
}

/// A concrete obligation instance: one rule firing at one trigger step.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Obligation {
    /// Stable unique id: `"{epoch}:{rule_id}:{trigger_step}"`.
    pub id: String,
    pub epoch: usize,
    pub rule_id: String,
    pub kind: Kind,
    pub trigger_step: i64,
    /// Inclusive window.
    pub window_start: i64,
    pub window_end: i64,
    pub status: ObligationStatus,
    pub reason: ObligationReason,
    pub satisfied_at: Option<i64>,
    pub failed_at: Option<i64>,
}

impl Obligation {
    /// Three-valued instance verdict. A monitor invariant guarantees no
    /// instance is pending after close, so callers see `wait` only on an
    /// open trace.
    pub fn verdict(&self) -> Verdict {
        match self.status {
            ObligationStatus::Satisfied => Verdict::Sat,
            ObligationStatus::Violated => Verdict::Viol,
            ObligationStatus::Pending => Verdict::Wait,
        }
    }
}

/// One append-only, hash-chained line of the decision journal.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DecisionEntry {
    pub seq: u64,
    pub run_id: String,
    /// `None` for trace/epoch boundaries.
    pub step: Option<i64>,
    #[serde(rename = "type")]
    pub etype: String,
    pub epoch: usize,
    pub obligation_id: Option<String>,
    pub rule_id: Option<String>,
    pub detail: Value,
    pub prev_hash: String,
    pub hash: String,
}

/// Bounds that turn unbounded inputs into a distinguishable resource error.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Limits {
    pub max_active_obligations: usize,
    pub max_epochs: usize,
    pub max_log_bytes: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_active_obligations: 256,
            max_epochs: 16,
            max_log_bytes: 1 << 20,
        }
    }
}

impl Limits {
    /// Generous bounds for offline replay (fixtures are small).
    pub fn replay() -> Self {
        Limits {
            max_active_obligations: 4096,
            max_epochs: 64,
            max_log_bytes: 64 << 20,
        }
    }
}

/// Result of feeding one event.
#[derive(Debug, Clone, Serialize)]
pub struct StepReport {
    pub step: i64,
    pub epoch: usize,
    pub version: String,
    pub spawned: Vec<String>,
    pub satisfied: Vec<Resolved>,
    pub violated: Vec<Resolved>,
    /// Three-valued global verdict *after* this step (may be `wait`).
    pub verdict: Verdict,
}

#[derive(Debug, Clone, Serialize)]
pub struct Resolved {
    pub obligation_id: String,
    pub rule_id: String,
    pub at: Option<i64>,
    pub reason: ObligationReason,
}

/// Per-rule roll-up used by status responses and fixtures.
#[derive(Debug, Clone, Serialize)]
pub struct RuleOutcome {
    pub epoch: usize,
    pub version: String,
    pub rule_id: String,
    pub kind: Kind,
    pub verdict: Verdict,
    pub instances: Vec<InstanceOutcome>,
}

#[derive(Debug, Clone, Serialize)]
pub struct InstanceOutcome {
    pub obligation_id: String,
    pub trigger_step: i64,
    pub window: [i64; 2],
    pub status: ObligationStatus,
    pub reason: ObligationReason,
    pub satisfied_at: Option<i64>,
    pub failed_at: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct MonitorStatus {
    pub monitor_id: String,
    pub run_id: String,
    pub closed: bool,
    pub next_step: i64,
    pub active_epoch: usize,
    pub version: String,
    pub global_verdict: Verdict,
    pub obligations_total: usize,
    pub obligations_pending: usize,
    pub epochs: Vec<String>,
}

/// Validate a ruleset in isolation (used by create/rotate and the reference
/// tooling before any state exists).
pub fn validate_ruleset(rs: &RuleSet) -> Result<()> {
    if rs.version.trim().is_empty() {
        return Err(KernelError::input(
            "BAD_VERSION",
            "ruleset version must be non-empty",
        ));
    }
    if rs.response.is_empty() && rs.sustain.is_empty() {
        return Err(KernelError::empty_ruleset());
    }
    let mut seen = std::collections::HashSet::new();
    let valid_id = |id: &str| {
        !id.is_empty()
            && id.len() <= 64
            && id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    };
    for r in &rs.response {
        if !valid_id(&r.id) {
            return Err(KernelError::bad_rule_id(&r.id));
        }
        if !seen.insert(r.id.clone()) {
            return Err(KernelError::duplicate_rule_id(&r.id));
        }
        if r.after < 0 || r.within < 1 {
            return Err(KernelError::bad_window(&r.id));
        }
    }
    for r in &rs.sustain {
        if !valid_id(&r.id) {
            return Err(KernelError::bad_rule_id(&r.id));
        }
        if !seen.insert(r.id.clone()) {
            return Err(KernelError::duplicate_rule_id(&r.id));
        }
        if r.after < 0 || r.duration < 1 {
            return Err(KernelError::bad_window(&r.id));
        }
    }
    Ok(())
}

/// The stateful monitor.
#[derive(Debug)]
pub struct Monitor {
    pub id: String,
    pub run_id: String,
    pub closed: bool,
    /// Next accepted (and next assigned) step index.
    pub next_step: i64,
    pub active_epoch: usize,
    /// Index equals epoch; append-only across rotations.
    pub rulesets: Vec<RuleSet>,
    /// Every obligation ever spawned, decided or pending.
    pub obligations: Vec<Obligation>,
    pub decisions: Vec<DecisionEntry>,
    pub next_seq: u64,
    pub log_bytes: usize,
    pub limits: Limits,
}

impl Monitor {
    pub fn new(id: String, run_id: String, ruleset: RuleSet, limits: Limits) -> Result<Self> {
        validate_ruleset(&ruleset)?;
        Ok(Monitor {
            id,
            run_id,
            closed: false,
            next_step: 0,
            active_epoch: 0,
            rulesets: vec![ruleset],
            obligations: Vec::new(),
            decisions: Vec::new(),
            next_seq: 0,
            log_bytes: 0,
            limits,
        })
    }

    pub fn version(&self) -> &str {
        &self.rulesets[self.active_epoch].version
    }

    fn ruleset(&self, epoch: usize) -> &RuleSet {
        &self.rulesets[epoch]
    }

    pub fn global_verdict(&self) -> Verdict {
        self.obligations
            .iter()
            .map(|o| o.verdict())
            .fold(Verdict::Sat, Verdict::merge)
    }

    pub fn pending_count(&self) -> usize {
        self.obligations
            .iter()
            .filter(|o| o.status == ObligationStatus::Pending)
            .count()
    }

    // ------------------------------------------------------------ feeding
    /// Feed one event. `step` must equal `next_step` when supplied.
    pub fn append(&mut self, ev: &Event, step: Option<i64>) -> Result<StepReport> {
        if self.closed {
            return Err(KernelError::monitor_closed());
        }
        if ev.attrs.is_empty() {
            return Err(KernelError::empty_event());
        }
        let step = match step {
            Some(s) if s < self.next_step => {
                return Err(KernelError::step_regressed(self.next_step, s))
            }
            Some(s) if s > self.next_step => return Err(KernelError::step_gap(self.next_step, s)),
            Some(s) => s,
            None => self.next_step,
        };
        let epoch = self.active_epoch;

        // ---- phase 1: spawns (pure plan, no mutation yet) ---------------
        let mut spawns: Vec<Obligation> = Vec::new();
        for r in &self.ruleset(epoch).response {
            if r.trigger.matches(ev) {
                spawns.push(Obligation {
                    id: format!("{}:{}:{}", epoch, r.id, step),
                    epoch,
                    rule_id: r.id.clone(),
                    kind: Kind::Response,
                    trigger_step: step,
                    window_start: step + r.after,
                    window_end: step + r.after + r.within - 1,
                    status: ObligationStatus::Pending,
                    reason: ObligationReason::Pending,
                    satisfied_at: None,
                    failed_at: None,
                });
            }
        }
        for r in &self.ruleset(epoch).sustain {
            if r.trigger.matches(ev) {
                spawns.push(Obligation {
                    id: format!("{}:{}:{}", epoch, r.id, step),
                    trigger_step: step,
                    window_start: step + r.after,
                    window_end: step + r.after + r.duration - 1,
                    epoch,
                    rule_id: r.id.clone(),
                    kind: Kind::Sustain,
                    status: ObligationStatus::Pending,
                    reason: ObligationReason::Pending,
                    satisfied_at: None,
                    failed_at: None,
                });
            }
        }
        if self.pending_count() + spawns.len() > self.limits.max_active_obligations {
            return Err(KernelError::obligation_limit(
                self.limits.max_active_obligations,
            ));
        }

        // ---- phase 2-6: effects on a draft obligation table -------------
        // Work on cloned pending obligations so the journal budget can still
        // reject the whole step atomically.
        let mut draft: Vec<Obligation> = self.obligations.clone();
        draft.extend(spawns.iter().cloned());

        let mut effects: Vec<Effect> = spawns.iter().map(|o| Effect::Spawned(o.clone())).collect();

        // 2: response matching (new spawns included => after=0 works).
        for r in &self.ruleset(epoch).response {
            if !r.response.matches(ev) {
                continue;
            }
            let mut eligible: Vec<&mut Obligation> = draft
                .iter_mut()
                .filter(|o| {
                    o.epoch == epoch
                        && o.kind == Kind::Response
                        && o.rule_id == r.id
                        && o.status == ObligationStatus::Pending
                        && o.window_start <= step
                        && step <= o.window_end
                })
                .collect();
            match r.satisfy {
                SatisfyMode::All => {
                    for o in eligible {
                        o.status = ObligationStatus::Satisfied;
                        o.reason = ObligationReason::Fulfilled;
                        o.satisfied_at = Some(step);
                        effects.push(Effect::Satisfied(o.id.clone(), r.id.clone(), step));
                    }
                }
                SatisfyMode::One => {
                    eligible.sort_by_key(|o| (o.window_start, o.trigger_step, o.id.clone()));
                    if let Some(o) = eligible.into_iter().next() {
                        o.status = ObligationStatus::Satisfied;
                        o.reason = ObligationReason::Fulfilled;
                        o.satisfied_at = Some(step);
                        effects.push(Effect::Satisfied(o.id.clone(), r.id.clone(), step));
                    }
                }
            }
        }

        // 3: response deadline sweep. The deadline step's own event was just
        // processed, so `window_end <= step` and still pending means missed.
        for o in draft.iter_mut() {
            if o.epoch == epoch
                && o.kind == Kind::Response
                && o.status == ObligationStatus::Pending
                && o.window_end <= step
            {
                o.status = ObligationStatus::Violated;
                o.reason = ObligationReason::DeadlineMissed;
                o.failed_at = Some(o.window_end);
                effects.push(Effect::Violated(
                    o.id.clone(),
                    o.rule_id.clone(),
                    ObligationReason::DeadlineMissed,
                    Some(o.window_end),
                ));
            }
        }

        // 4: sustain condition check at this step.
        for r in &self.ruleset(epoch).sustain {
            let held = r.sustain.matches(ev);
            for o in draft.iter_mut() {
                if o.epoch != epoch
                    || o.kind != Kind::Sustain
                    || o.rule_id != r.id
                    || o.status != ObligationStatus::Pending
                    || step < o.window_start
                    || step > o.window_end
                {
                    continue;
                }
                if !held {
                    o.status = ObligationStatus::Violated;
                    o.reason = ObligationReason::ConditionFailed;
                    o.failed_at = Some(step);
                    effects.push(Effect::Violated(
                        o.id.clone(),
                        r.id.clone(),
                        ObligationReason::ConditionFailed,
                        Some(step),
                    ));
                }
            }
        }

        // 5: sustain windows fully observed => satisfied.
        for o in draft.iter_mut() {
            if o.epoch == epoch
                && o.kind == Kind::Sustain
                && o.status == ObligationStatus::Pending
                && o.window_end <= step
            {
                o.status = ObligationStatus::Satisfied;
                o.reason = ObligationReason::Fulfilled;
                o.satisfied_at = Some(o.window_end);
                effects.push(Effect::Satisfied(
                    o.id.clone(),
                    o.rule_id.clone(),
                    o.window_end,
                ));
            }
        }

        // ---- journal: draft entries, enforce budget, commit atomically --
        let mut entries: Vec<DraftEntry> = Vec::new();
        entries.push(DraftEntry {
            etype: "event".into(),
            obligation_id: None,
            rule_id: None,
            detail: json!({"event": ev}),
            extra: json!({"step": step}),
        });
        for e in &effects {
            entries.push(e.into());
        }
        let ctx = JournalCtx {
            step: Some(step),
            epoch,
        };
        self.reserve_log(&entries, &ctx)?;
        self.commit_entries(&entries, &ctx);
        self.obligations = draft;
        self.next_step = step + 1;

        let mut spawned = Vec::new();
        let mut satisfied = Vec::new();
        let mut violated = Vec::new();
        for e in effects {
            match e {
                Effect::Spawned(o) => spawned.push(o.id),
                Effect::Satisfied(id, rid, at) => satisfied.push(Resolved {
                    obligation_id: id,
                    rule_id: rid,
                    at: Some(at),
                    reason: ObligationReason::Fulfilled,
                }),
                Effect::Violated(id, rid, reason, at) => violated.push(Resolved {
                    obligation_id: id,
                    rule_id: rid,
                    at,
                    reason,
                }),
            }
        }

        Ok(StepReport {
            step,
            epoch,
            version: self.ruleset(epoch).version.clone(),
            spawned,
            satisfied,
            violated,
            verdict: self.global_verdict(),
        })
    }

    // ------------------------------------------------------------ closing
    /// Close the trace. Every still-pending obligation is sealed with the
    /// rule's `on_close` policy.
    pub fn end(&mut self) -> Result<CloseReport> {
        if self.closed {
            return Err(KernelError::monitor_closed());
        }
        let last_step = self.next_step - 1;
        let epoch = self.active_epoch;
        let effects = self.close_pending(epoch, last_step);

        let detail = json!({"last_step": last_step, "sealed": effects.len()});
        // Sealing entries first, then the boundary entry.
        let mut all: Vec<DraftEntry> = effects.iter().map(DraftEntry::from).collect();
        all.push(DraftEntry {
            etype: "trace_closed".into(),
            obligation_id: None,
            rule_id: None,
            detail,
            extra: json!({}),
        });
        let ctx = JournalCtx { step: None, epoch };
        self.reserve_log(&all, &ctx)?;
        self.commit_entries(&all, &ctx);
        self.closed = true;

        Ok(CloseReport {
            sealed: effects.len(),
            last_step,
            verdict: self.global_verdict(),
        })
    }

    /// Seal pending obligations of one epoch at a boundary. Used by both
    /// `end` and `rotate`.
    fn close_pending(&mut self, epoch: usize, boundary_step: i64) -> Vec<Effect> {
        let rs = self.ruleset(epoch).clone();
        let mut effects = Vec::new();
        for o in self.obligations.iter_mut() {
            if o.epoch != epoch || o.status != ObligationStatus::Pending {
                continue;
            }
            let policy = match o.kind {
                Kind::Response => rs
                    .response
                    .iter()
                    .find(|r| r.id == o.rule_id)
                    .map(|r| r.on_close)
                    .unwrap_or_default(),
                Kind::Sustain => rs
                    .sustain
                    .iter()
                    .find(|r| r.id == o.rule_id)
                    .map(|r| r.on_close)
                    .unwrap_or_default(),
            };
            match (o.kind, policy) {
                (_, ClosePolicy::Lenient) => {
                    o.status = ObligationStatus::Satisfied;
                    o.reason = ObligationReason::ClosedAccepted;
                    o.satisfied_at = Some(boundary_step);
                    effects.push(Effect::Satisfied(
                        o.id.clone(),
                        o.rule_id.clone(),
                        boundary_step,
                    ));
                }
                (Kind::Response, ClosePolicy::Strict) => {
                    o.status = ObligationStatus::Violated;
                    o.reason = ObligationReason::ClosedPending;
                    o.failed_at = Some(boundary_step);
                    effects.push(Effect::Violated(
                        o.id.clone(),
                        o.rule_id.clone(),
                        ObligationReason::ClosedPending,
                        Some(boundary_step),
                    ));
                }
                (Kind::Sustain, ClosePolicy::Strict) => {
                    o.status = ObligationStatus::Violated;
                    o.reason = ObligationReason::ClosedIncomplete;
                    o.failed_at = Some(boundary_step);
                    effects.push(Effect::Violated(
                        o.id.clone(),
                        o.rule_id.clone(),
                        ObligationReason::ClosedIncomplete,
                        Some(boundary_step),
                    ));
                }
            }
        }
        effects
    }

    /// Rotate to a new ruleset version. Pending obligations of the old epoch
    /// are sealed at the boundary; the new epoch starts with a clean
    /// obligation table while history remains for evidence.
    pub fn rotate(&mut self, new_ruleset: RuleSet) -> Result<RotateReport> {
        if self.closed {
            return Err(KernelError::monitor_closed());
        }
        validate_ruleset(&new_ruleset)?;
        if new_ruleset.version == self.version() {
            return Err(KernelError::input(
                "SAME_VERSION",
                "rotated ruleset must have a different version",
            ));
        }
        if self.active_epoch + 1 >= self.limits.max_epochs {
            return Err(KernelError::epoch_limit(self.limits.max_epochs));
        }
        let boundary_step = self.next_step - 1;
        let epoch = self.active_epoch;
        let from_version = self.ruleset(epoch).version.clone();
        let effects = self.close_pending(epoch, boundary_step);

        let mut entries: Vec<DraftEntry> = effects.iter().map(DraftEntry::from).collect();
        entries.push(DraftEntry {
            etype: "epoch_rotated".into(),
            obligation_id: None,
            rule_id: None,
            detail: json!({
                "from_version": from_version,
                "to_version": new_ruleset.version,
                "sealed": effects.len(),
                "boundary_step": boundary_step,
            }),
            extra: json!({}),
        });
        let ctx = JournalCtx { step: None, epoch };
        self.reserve_log(&entries, &ctx)?;
        self.commit_entries(&entries, &ctx);

        self.rulesets.push(new_ruleset);
        self.active_epoch += 1;

        Ok(RotateReport {
            new_epoch: self.active_epoch,
            version: self.version().to_string(),
            sealed: effects.len(),
            verdict: self.global_verdict(),
        })
    }

    // ------------------------------------------------------------ journal
    /// Build the canonical payload (without `hash`) for one draft entry.
    fn entry_payload(&self, ctx: &JournalCtx, e: &DraftEntry, seq: u64, prev_hash: &str) -> Value {
        let mut m = Map::new();
        m.insert("seq".into(), json!(seq));
        m.insert("run_id".into(), json!(self.run_id));
        m.insert("step".into(), json!(ctx.step));
        m.insert("type".into(), json!(e.etype));
        m.insert("epoch".into(), json!(ctx.epoch));
        m.insert("obligation_id".into(), json!(e.obligation_id));
        m.insert("rule_id".into(), json!(e.rule_id));
        m.insert("detail".into(), e.detail.clone());
        if let Some(obj) = e.extra.as_object() {
            for (k, v) in obj {
                m.insert(k.clone(), v.clone());
            }
        }
        m.insert("prev_hash".into(), json!(prev_hash));
        Value::Object(m)
    }

    /// Size-check a batch of pending entries against the log budget without
    /// mutating state. Entries are measured exactly as they will be stored.
    fn reserve_log(&self, entries: &[DraftEntry], ctx: &JournalCtx) -> Result<()> {
        let mut prev = self
            .decisions
            .last()
            .map(|e| e.hash.clone())
            .unwrap_or_else(|| "GENESIS".to_string());
        let mut added = 0usize;
        for (i, e) in entries.iter().enumerate() {
            let payload = self.entry_payload(ctx, e, self.next_seq + i as u64, &prev);
            let hash = sha256_hex(&canonical_bytes(&payload));
            let mut with_hash = payload;
            with_hash
                .as_object_mut()
                .unwrap()
                .insert("hash".into(), json!(hash));
            added += canonical_bytes(&with_hash).len() + 1; // +1 newline
            prev = hash;
        }
        if self.log_bytes + added > self.limits.max_log_bytes {
            return Err(KernelError::log_limit(self.limits.max_log_bytes));
        }
        Ok(())
    }

    fn commit_entries(&mut self, entries: &[DraftEntry], ctx: &JournalCtx) {
        for e in entries {
            let seq = self.next_seq;
            let prev = self
                .decisions
                .last()
                .map(|e| e.hash.clone())
                .unwrap_or_else(|| "GENESIS".to_string());
            let payload = self.entry_payload(ctx, e, seq, &prev);
            let hash = sha256_hex(&canonical_bytes(&payload));
            let stored = DecisionEntry {
                seq,
                run_id: self.run_id.clone(),
                step: ctx.step,
                etype: e.etype.clone(),
                epoch: ctx.epoch,
                obligation_id: e.obligation_id.clone(),
                rule_id: e.rule_id.clone(),
                detail: e.detail.clone(),
                prev_hash: prev,
                hash: hash.clone(),
            };
            self.log_bytes += canonical_bytes(&serde_json::to_value(&stored).unwrap()).len() + 1;
            self.decisions.push(stored);
            self.next_seq += 1;
        }
    }

    /// Recompute the full hash chain; returns the first broken seq.
    pub fn verify_chain(&self) -> Result<()> {
        let mut prev = "GENESIS".to_string();
        for (i, e) in self.decisions.iter().enumerate() {
            if e.seq as usize != i {
                return Err(KernelError::computation(format!(
                    "decision seq mismatch at index {i}: got {}",
                    e.seq
                )));
            }
            if e.prev_hash != prev {
                return Err(KernelError::decision_chain_broken());
            }
            let mut v = serde_json::to_value(e).map_err(|err| {
                KernelError::computation(format!("decision serialisation failed: {err}"))
            })?;
            v.as_object_mut().unwrap().remove("hash");
            if sha256_hex(&canonical_bytes(&v)) != e.hash {
                return Err(KernelError::decision_chain_broken());
            }
            prev = e.hash.clone();
        }
        Ok(())
    }

    // ------------------------------------------------------------ readings
    pub fn status(&self) -> MonitorStatus {
        MonitorStatus {
            monitor_id: self.id.clone(),
            run_id: self.run_id.clone(),
            closed: self.closed,
            next_step: self.next_step,
            active_epoch: self.active_epoch,
            version: self.version().to_string(),
            global_verdict: self.global_verdict(),
            obligations_total: self.obligations.len(),
            obligations_pending: self.pending_count(),
            epochs: self.rulesets.iter().map(|r| r.version.clone()).collect(),
        }
    }

    /// Per-rule outcomes, one entry per (epoch, rule) — including rules that
    /// never fired (vacuous `sat`).
    pub fn rule_outcomes(&self) -> Vec<RuleOutcome> {
        let mut out = Vec::new();
        for (epoch, rs) in self.rulesets.iter().enumerate() {
            for r in &rs.response {
                out.push(self.outcome_for(epoch, &rs.version, &r.id, Kind::Response));
            }
            for r in &rs.sustain {
                out.push(self.outcome_for(epoch, &rs.version, &r.id, Kind::Sustain));
            }
        }
        out
    }

    fn outcome_for(&self, epoch: usize, version: &str, rule_id: &str, kind: Kind) -> RuleOutcome {
        let instances: Vec<InstanceOutcome> = self
            .obligations
            .iter()
            .filter(|o| o.epoch == epoch && o.rule_id == rule_id && o.kind == kind)
            .map(|o| InstanceOutcome {
                obligation_id: o.id.clone(),
                trigger_step: o.trigger_step,
                window: [o.window_start, o.window_end],
                status: o.status,
                reason: o.reason,
                satisfied_at: o.satisfied_at,
                failed_at: o.failed_at,
            })
            .collect();
        let verdict = instances
            .iter()
            .map(|i| match i.status {
                ObligationStatus::Satisfied => Verdict::Sat,
                ObligationStatus::Violated => Verdict::Viol,
                ObligationStatus::Pending => Verdict::Wait,
            })
            .fold(Verdict::Sat, Verdict::merge);
        RuleOutcome {
            epoch,
            version: version.to_string(),
            rule_id: rule_id.to_string(),
            kind,
            verdict,
            instances,
        }
    }

    // --------------------------------------------------------- snapshots
    pub fn snapshot(&self) -> Value {
        let mut snap = json!({
            "monitor_id": self.id,
            "run_id": self.run_id,
            "closed": self.closed,
            "next_step": self.next_step,
            "active_epoch": self.active_epoch,
            "rulesets": self.rulesets,
            "obligations": self.obligations,
            "decisions": self.decisions,
            "next_seq": self.next_seq,
            "log_bytes": self.log_bytes,
            "limits": self.limits,
        });
        let digest = canonical_digest(&snap);
        snap.as_object_mut()
            .unwrap()
            .insert("digest".into(), json!(digest));
        snap
    }

    /// Restore from a snapshot value, verifying digest, chain and internal
    /// cross-references. `expected_version` triggers a state conflict when
    /// the caller's ruleset version does not match the snapshot's active one.
    pub fn restore(snap: Value, expected_version: Option<&str>) -> Result<Self> {
        let claimed = snap
            .get("digest")
            .and_then(|v| v.as_str())
            .ok_or_else(|| KernelError::input("BAD_SNAPSHOT", "snapshot has no digest"))?;
        if claimed != canonical_digest(&snap) {
            return Err(KernelError::snapshot_digest());
        }
        #[derive(Deserialize)]
        struct SnapData {
            monitor_id: String,
            run_id: String,
            closed: bool,
            next_step: i64,
            active_epoch: usize,
            rulesets: Vec<RuleSet>,
            obligations: Vec<Obligation>,
            decisions: Vec<DecisionEntry>,
            next_seq: u64,
            log_bytes: usize,
            limits: Limits,
        }
        let data: SnapData = serde_json::from_value(snap)
            .map_err(|e| KernelError::input("BAD_SNAPSHOT", format!("malformed snapshot: {e}")))?;

        for (i, rs) in data.rulesets.iter().enumerate() {
            validate_ruleset(rs).map_err(|e| {
                KernelError::computation(format!("snapshot epoch {i} failed validation: {e}"))
            })?;
        }
        if data.active_epoch >= data.rulesets.len() {
            return Err(KernelError::computation(
                "active_epoch out of range in snapshot",
            ));
        }
        if let Some(want) = expected_version {
            let have = &data.rulesets[data.active_epoch].version;
            if want != have {
                return Err(KernelError::version_mismatch(have, want));
            }
        }
        // Obligations reference rules existing in their birth epoch.
        for o in &data.obligations {
            let rs = &data.rulesets[o.epoch];
            let known = match o.kind {
                Kind::Response => rs.response.iter().any(|r| r.id == o.rule_id),
                Kind::Sustain => rs.sustain.iter().any(|r| r.id == o.rule_id),
            };
            if !known {
                return Err(KernelError::computation(format!(
                    "obligation {} references missing rule {} in epoch {}",
                    o.id, o.rule_id, o.epoch
                )));
            }
        }
        if data.closed
            && data
                .obligations
                .iter()
                .any(|o| o.status == ObligationStatus::Pending)
        {
            return Err(KernelError::computation(
                "closed snapshot still contains pending obligations",
            ));
        }
        let m = Monitor {
            id: data.monitor_id,
            run_id: data.run_id,
            closed: data.closed,
            next_step: data.next_step,
            active_epoch: data.active_epoch,
            rulesets: data.rulesets,
            obligations: data.obligations,
            decisions: data.decisions,
            next_seq: data.next_seq,
            log_bytes: data.log_bytes,
            limits: data.limits,
        };
        m.verify_chain()?;
        if m.next_seq as usize != m.decisions.len() {
            return Err(KernelError::computation(
                "next_seq disagrees with decision count",
            ));
        }
        Ok(m)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct CloseReport {
    pub sealed: usize,
    pub last_step: i64,
    pub verdict: Verdict,
}

#[derive(Debug, Clone, Serialize)]
pub struct RotateReport {
    pub new_epoch: usize,
    pub version: String,
    pub sealed: usize,
    pub verdict: Verdict,
}

/// Internal, pre-commit state transition used to build both the draft
/// obligation table and the journal entries in one pass.
enum Effect {
    Spawned(Obligation),
    Satisfied(String, String, i64),
    Violated(String, String, ObligationReason, Option<i64>),
}

/// Step/epoch context shared by every journal entry committed in one batch.
#[derive(Debug, Clone, Copy)]
struct JournalCtx {
    step: Option<i64>,
    epoch: usize,
}

/// A journal entry awaiting hashing and commit.
#[derive(Debug, Clone)]
struct DraftEntry {
    etype: String,
    obligation_id: Option<String>,
    rule_id: Option<String>,
    detail: Value,
    /// Additional top-level fields merged into the stored entry.
    extra: Value,
}

impl From<&Effect> for DraftEntry {
    fn from(e: &Effect) -> Self {
        let (etype, obligation_id, rule_id, detail) = match e {
            Effect::Spawned(o) => (
                "obligation_spawned".to_string(),
                Some(o.id.clone()),
                Some(o.rule_id.clone()),
                json!({
                    "kind": o.kind,
                    "trigger_step": o.trigger_step,
                    "window": [o.window_start, o.window_end],
                    "epoch": o.epoch,
                }),
            ),
            Effect::Satisfied(id, rid, at) => (
                "obligation_satisfied".to_string(),
                Some(id.clone()),
                Some(rid.clone()),
                json!({"at": at}),
            ),
            Effect::Violated(id, rid, reason, at) => (
                "obligation_violated".to_string(),
                Some(id.clone()),
                Some(rid.clone()),
                json!({"reason": reason, "at": at}),
            ),
        };
        DraftEntry {
            etype,
            obligation_id,
            rule_id,
            detail,
            extra: json!({}),
        }
    }
}
