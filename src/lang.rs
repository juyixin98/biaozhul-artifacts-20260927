//! Input language and shared result vocabulary.
//!
//! # Language
//! A [`RuleSet`] binds together:
//! - `response` rules: when a trigger fires, an event matching the response
//!   atom must occur inside the closed window `[t+after, t+after+within-1]`.
//! - `sustain` rules: after a trigger fires, the sustain condition must hold
//!   at **every** step of the closed window `[t+after, t+after+duration-1]`
//!   (duration counts inclusively from the first covered step).
//!
//! Atoms compare one event attribute (`field`) against a JSON value with a
//! fixed set of typed operators. Conditions are conjunctions of atoms
//! (`all: [...]`); an absent or `null` attribute never matches.
//!
//! # Verdicts
//! Everything is three-valued: [`Verdict::Sat`], [`Verdict::Viol`] and
//! [`Verdict::Wait`]. `Wait` means "not yet decided on an open trace" and must
//! never be reported as satisfaction.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Atomic predicate over a single event: `field <op> value`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Atom {
    pub field: String,
    pub op: Op,
    pub value: Value,
}

/// Supported comparison operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Op {
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    In,
}

impl Atom {
    /// Evaluate the atom against one event's payload.
    ///
    /// Missing attributes and type-mismatched ordering comparisons evaluate
    /// to `false` (closed-world per event); `in` expects a JSON array.
    pub fn matches(&self, ev: &Event) -> bool {
        let Some(actual) = ev.attrs.get(&self.field) else {
            return false;
        };
        match self.op {
            Op::Eq => actual == &self.value,
            Op::Ne => actual != &self.value,
            Op::In => self
                .value
                .as_array()
                .is_some_and(|xs| xs.iter().any(|x| x == actual)),
            Op::Lt => json_cmp(actual, &self.value).is_some_and(|o| o.is_lt()),
            Op::Le => json_cmp(actual, &self.value).is_some_and(|o| o.is_le()),
            Op::Gt => json_cmp(actual, &self.value).is_some_and(|o| o.is_gt()),
            Op::Ge => json_cmp(actual, &self.value).is_some_and(|o| o.is_ge()),
        }
    }
}

/// Conjunction condition. `all` is the only combinator in v1; an empty list
/// is `true` (fires on every event).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Condition {
    #[serde(default)]
    pub all: Vec<Atom>,
}

impl Condition {
    pub fn matches(&self, ev: &Event) -> bool {
        self.all.iter().all(|a| a.matches(ev))
    }
}

/// How one response event relates to several simultaneously pending
/// obligations of the same rule.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SatisfyMode {
    /// One matching response event fulfils **every** pending obligation it
    /// matches (default). Obligations are tracked independently; the same
    /// event is allowed to discharge many of them.
    #[default]
    All,
    /// One matching event discharges only the earliest-eligible pending
    /// obligation; the others keep waiting.
    One,
}

/// What happens to obligations still pending when a trace/epoch is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClosePolicy {
    /// Pending response obligations and not-fully-observed sustain windows
    /// are violations (default).
    #[default]
    Strict,
    /// Premature closure is treated as satisfaction. The reason code still
    /// records that closure was premature, so the two cases stay separable.
    Lenient,
}

/// Respond-within-N rule.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResponseRule {
    pub id: String,
    pub trigger: Condition,
    /// A single atom identifies the response event (existential per step).
    pub response: Atom,
    /// Window start offset from the trigger step (0 = same step allowed).
    #[serde(default)]
    pub after: i64,
    /// Window length in steps; deadline is `trigger + after + within - 1`.
    pub within: i64,
    #[serde(default)]
    pub satisfy: SatisfyMode,
    #[serde(default)]
    pub on_close: ClosePolicy,
}

/// Sustain-for-K rule.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SustainRule {
    pub id: String,
    pub trigger: Condition,
    /// Condition that must hold at every step inside the window.
    pub sustain: Condition,
    #[serde(default)]
    pub after: i64,
    /// Number of steps the condition must hold, including the first window
    /// step. Window: `[trigger+after, trigger+after+duration-1]`.
    pub duration: i64,
    #[serde(default)]
    pub on_close: ClosePolicy,
}

/// A versioned, immutable bundle of rules. Rotating to a new ruleset starts a
/// fresh epoch; old obligations carry their epoch and never read new rules.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuleSet {
    pub version: String,
    #[serde(default)]
    pub response: Vec<ResponseRule>,
    #[serde(default)]
    pub sustain: Vec<SustainRule>,
}

/// One observed trace element. `step` is assigned by the service when absent;
/// clients that supply it must use contiguous steps starting at the
/// monitor's expected index. `kind` is a conventional attribute stored like
/// every other attribute so trigger/response atoms can match on it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Event {
    #[serde(flatten)]
    pub attrs: serde_json::Map<String, Value>,
}

impl Event {
    /// Convenience constructor: `Event::new("payment")` with extra attrs.
    pub fn new(kind: impl Into<String>) -> Self {
        let mut attrs = serde_json::Map::new();
        attrs.insert("kind".to_string(), Value::String(kind.into()));
        Event { attrs }
    }

    /// Builder-style attribute insertion.
    pub fn with(mut self, k: impl Into<String>, v: impl Into<Value>) -> Self {
        self.attrs.insert(k.into(), v.into());
        self
    }
}

/// Three-valued verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Sat,
    Viol,
    Wait,
}

impl Verdict {
    /// Lattice merge: any violation dominates, otherwise wait dominates sat.
    pub fn merge(self, other: Verdict) -> Verdict {
        match (self, other) {
            (Verdict::Viol, _) | (_, Verdict::Viol) => Verdict::Viol,
            (Verdict::Wait, _) | (_, Verdict::Wait) => Verdict::Wait,
            _ => Verdict::Sat,
        }
    }
    pub fn is_decided(self) -> bool {
        self != Verdict::Wait
    }
}

/// Terminal/pending status of one concrete obligation instance.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObligationStatus {
    Pending,
    Satisfied,
    Violated,
}

/// Machine-readable reason explaining an obligation's state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObligationReason {
    /// Response: matched at `satisfied_at`; Sustain: condition held all window.
    Fulfilled,
    /// Response: deadline step passed with no match.
    DeadlineMissed,
    /// Sustain: condition failed at one step (`failed_at`).
    ConditionFailed,
    /// Closed while pending; strict policy.
    ClosedPending,
    /// Sustain window not fully observable at close; strict policy.
    ClosedIncomplete,
    /// Closed pending/incomplete under the lenient policy (accepted).
    ClosedAccepted,
    /// Still open on an unfinished trace.
    Pending,
}

/// Compare two JSON numbers; non-numbers yield `None`.
fn json_cmp(a: &Value, b: &Value) -> Option<std::cmp::Ordering> {
    match (a.as_f64(), b.as_f64()) {
        (Some(x), Some(y)) => x.partial_cmp(&y),
        _ => None,
    }
}
