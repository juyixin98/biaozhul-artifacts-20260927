//! Input language: rule sets, predicates and trace steps.
//!
//! All types are plain serde DTOs with no monitoring state, so the offline
//! oracle (see `oracle.rs`) and the online kernel (`kernel.rs`) share the
//! exact same parsing of the language without sharing any obligation logic.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::error::{AppError, AppResult};

/// Max nesting depth accepted inside a predicate (And/Or/Not), enforced at
/// rule-set validation time.
pub const MAX_PREDICATE_DEPTH: usize = 16;

/// A rule set: a versioned collection of temporal rules.
///
/// `version` participates in every serialized monitor state; restoring state
/// produced under a different rule-set version (or different rule content,
/// via the content hash) is rejected as a state conflict so old rule state
/// can never be mixed with new rules.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ruleset {
    pub id: String,
    pub version: String,
    #[serde(default)]
    pub rules: Vec<RuleDef>,
}

/// One temporal rule.  Exactly one policy is present; this is encoded with an
/// internally tagged enum (`"type": "response" | "sustain"`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum RuleDef {
    Response(ResponseRuleDef),
    Sustain(SustainRuleDef),
}

impl RuleDef {
    pub fn id(&self) -> &str {
        match self {
            RuleDef::Response(r) => &r.id,
            RuleDef::Sustain(r) => &r.id,
        }
    }
}

/// How one response event resolves several *same-rule* obligations that match
/// it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConsumePolicy {
    /// The response satisfies the single pending obligation with the earliest
    /// deadline (ties broken by earliest trigger step, then by obligation id).
    EarliestDeadline,
    /// The response satisfies *every* pending obligation of this rule that
    /// matches it.
    AllMatching,
}

/// Respond-within-N rule:
/// whenever an event matches `trigger`, later events must include one
/// matching `response` within `within_steps` subsequent steps.
///
/// A correlation key may be declared; when present, trigger and response are
/// only paired if they carry equal fact values at `correlation_key`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResponseRuleDef {
    pub id: String,
    pub trigger: EventPattern,
    pub response: EventPattern,
    pub within_steps: u64,
    #[serde(default = "default_consume")]
    pub consume: ConsumePolicy,
    /// Dotted fact path, e.g. `order.id`.  Optional.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub correlation_key: Option<String>,
}

fn default_consume() -> ConsumePolicy {
    ConsumePolicy::AllMatching
}

/// Hold-for-K rule:
/// once (or always, depending on `scope`) an event matching `condition`
/// occurs, that condition must hold continuously for `duration_steps`
/// consecutive steps.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SustainRuleDef {
    pub id: String,
    pub condition: Predicate,
    pub duration_steps: u64,
    #[serde(default = "default_scope")]
    pub scope: SustainScope,
}

fn default_scope() -> SustainScope {
    SustainScope::Always
}

/// `always` evaluates the condition at every step of the trace (vacuously
/// satisfied on an empty trace).  `after_trigger` starts a fresh hold window
/// whenever `trigger` matches.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum SustainScope {
    Always,
    AfterTrigger { trigger: EventPattern },
}

/// Predicate matching either an event type or an event type plus a fact
/// condition.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventPattern {
    pub event_type: String,
    #[serde(default, skip_serializing_if = "Option::is_none", rename = "where")]
    pub where_: Option<Predicate>,
}

/// Boolean/fact predicates over a step's merged fact map.
///
/// Fact paths are dotted (`a.b`); every step carries one event with a flat
/// `facts` object, and the event fields `type`, `index`, `at` are also
/// addressable.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Predicate {
    #[serde(rename_all = "snake_case")]
    Eq { path: String, value: serde_json::Value },
    #[serde(rename_all = "snake_case")]
    Ne { path: String, value: serde_json::Value },
    #[serde(rename_all = "snake_case")]
    Gt { path: String, value: serde_json::Value },
    #[serde(rename_all = "snake_case")]
    Lt { path: String, value: serde_json::Value },
    #[serde(rename_all = "snake_case")]
    Gte { path: String, value: serde_json::Value },
    #[serde(rename_all = "snake_case")]
    Lte { path: String, value: serde_json::Value },
    In {
        path: String,
        values: Vec<serde_json::Value>,
    },
    Exists {
        path: String,
    },
    Bool {
        path: String,
    },
    Not {
        inner: Box<Predicate>,
    },
    And {
        all: Vec<Predicate>,
    },
    Or {
        any: Vec<Predicate>,
    },
}

impl Predicate {
    fn depth(&self) -> usize {
        match self {
            Predicate::Not { inner } => 1 + inner.depth(),
            Predicate::And { all } => 1 + all.iter().map(Predicate::depth).max().unwrap_or(0),
            Predicate::Or { any } => 1 + any.iter().map(Predicate::depth).max().unwrap_or(0),
            _ => 1,
        }
    }

    fn validate(&self) -> AppResult<()> {
        if self.depth() > MAX_PREDICATE_DEPTH {
            return Err(AppError::input(
                "predicate_too_deep",
                format!("predicate depth {} exceeds {}", self.depth(), MAX_PREDICATE_DEPTH),
            ));
        }
        match self {
            Predicate::Not { inner } => inner.validate(),
            Predicate::And { all } => {
                if all.is_empty() {
                    return Err(AppError::input("empty_and", "`and` requires at least one term"));
                }
                for p in all {
                    p.validate()?;
                }
                Ok(())
            }
            Predicate::Or { any } => {
                if any.is_empty() {
                    return Err(AppError::input("empty_or", "`or` requires at least one term"));
                }
                for p in any {
                    p.validate()?;
                }
                Ok(())
            }
            Predicate::In { values, .. } => {
                if values.is_empty() {
                    return Err(AppError::input("empty_in", "`in` requires at least one value"));
                }
                Ok(())
            }
            _ => Ok(()),
        }
    }
}

/// One trace step.  Steps must be submitted with strictly increasing
/// contiguous `index` values starting at 0; an explicit `end: true` closes
/// the finite trace.  `ruleset_version` optionally pins the step to a
/// specific rule-set version.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Step {
    pub index: u64,
    #[serde(default)]
    pub event: Event,
    #[serde(default)]
    pub ruleset_version: Option<String>,
    #[serde(default)]
    pub end: bool,
}

/// The observed event at a step.  `facts` is a flat JSON object whose
/// top-level values may themselves be objects (addressed by dotted paths).
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Event {
    #[serde(rename = "type", default)]
    pub event_type: String,
    #[serde(default)]
    pub facts: serde_json::Map<String, serde_json::Value>,
}

/// Flattened view of a step used during predicate evaluation: the event's
/// `type` and facts, with `type` / `index` pseudo-paths also addressable.
#[derive(Debug, Clone)]
pub struct StepView<'a> {
    pub index: u64,
    pub event_type: &'a str,
    pub facts: &'a serde_json::Map<String, serde_json::Value>,
}

impl<'a> StepView<'a> {
    /// Resolve a dotted path against the step.
    ///
    /// The pseudo paths `type` and `index` expose the event type and step
    /// index; other top-level keys resolve in the event facts (which may
    /// contain nested objects).  Returns `None` when any segment is missing
    /// or traverses a non-object.  Values are cloned: fact payloads in this
    /// bounded monitor are small, and owning the result lets the pseudo
    /// paths work without special cases.
    pub fn lookup(&self, path: &str) -> Option<serde_json::Value> {
        let mut segments = path.split('.');
        let first = segments.next()?;
        let start: serde_json::Value = match first {
            "type" => serde_json::Value::String(self.event_type.to_string()),
            "index" => serde_json::json!(self.index),
            key => self.facts.get(key)?.clone(),
        };
        segments.try_fold(start, |acc, seg| acc.get(seg).cloned())
    }
}

impl Ruleset {
    /// Validate every field of the rule set.  Called on creation and on any
    /// request that embeds a rule set, so invalid input can never reach the
    /// kernel.
    pub fn validate(&self) -> AppResult<()> {
        if self.id.trim().is_empty() {
            return Err(AppError::input("empty_ruleset_id", "ruleset id must be non-empty"));
        }
        if self.version.trim().is_empty() {
            return Err(AppError::input("empty_ruleset_version", "ruleset version must be non-empty"));
        }
        if self.rules.is_empty() {
            return Err(AppError::input("empty_rules", "a ruleset needs at least one rule"));
        }
        let mut seen = std::collections::HashSet::new();
        for rule in &self.rules {
            let id = rule.id();
            if id.trim().is_empty() {
                return Err(AppError::input("empty_rule_id", "rule id must be non-empty"));
            }
            if !seen.insert(id) {
                return Err(AppError::input(
                    "duplicate_rule_id",
                    format!("rule id `{id}` occurs more than once"),
                ));
            }
            match rule {
                RuleDef::Response(r) => {
                    validate_pattern(&r.trigger)?;
                    validate_pattern(&r.response)?;
                    if r.within_steps == 0 {
                        return Err(AppError::input(
                            "zero_window",
                            format!("response rule `{id}` needs within_steps >= 1"),
                        ));
                    }
                    if let Some(key) = &r.correlation_key {
                        if key.trim().is_empty() {
                            return Err(AppError::input(
                                "empty_correlation_key",
                                format!("response rule `{id}` has an empty correlation_key"),
                            ));
                        }
                    }
                }
                RuleDef::Sustain(r) => {
                    r.condition.validate()?;
                    if r.duration_steps == 0 {
                        return Err(AppError::input(
                            "zero_duration",
                            format!("sustain rule `{id}` needs duration_steps >= 1"),
                        ));
                    }
                    if let SustainScope::AfterTrigger { trigger } = &r.scope {
                        validate_pattern(trigger)?;
                    }
                }
            }
        }
        Ok(())
    }

    /// Stable canonical serialization used for the content hash.
    /// `BTreeMap` gives field-order-independent output because serde_json
    /// already serializes struct fields in declaration order and the input
    /// maps here are rule arrays (order kept); BTreeMap is used for any
    /// fact maps that get hashed elsewhere.
    pub fn canonical_json(&self) -> Vec<u8> {
        let ordered: OrderedRuleset = OrderedRuleset::from(self.clone());
        serde_json::to_vec(&ordered).expect("ruleset serialization cannot fail")
    }
}

#[derive(Serialize)]
struct OrderedRuleset {
    id: String,
    version: String,
    rules: Vec<RuleDef>,
}

impl From<Ruleset> for OrderedRuleset {
    fn from(r: Ruleset) -> Self {
        Self { id: r.id, version: r.version, rules: r.rules }
    }
}

fn validate_pattern(p: &EventPattern) -> AppResult<()> {
    if p.event_type.trim().is_empty() {
        return Err(AppError::input("empty_event_type", "event pattern type must be non-empty"));
    }
    if let Some(w) = &p.where_ {
        w.validate()?;
    }
    Ok(())
}

impl Step {
    /// Structural validation of an incoming step.
    pub fn validate(&self) -> AppResult<()> {
        if self.event.event_type.trim().is_empty() {
            return Err(AppError::input("empty_event_type", "step event type must be non-empty"));
        }
        Ok(())
    }
}

/// Build a [`StepView`] fact lookup helper from a step.
pub fn step_view(step: &Step) -> StepView<'_> {
    StepView { index: step.index, event_type: &step.event.event_type, facts: &step.event.facts }
}

/// Convenience: convert a flat map into a BTreeMap (used by fixtures/tests
/// when constructing facts deterministically).
pub fn ordered_facts(
    facts: serde_json::Map<String, serde_json::Value>,
) -> BTreeMap<String, serde_json::Value> {
    facts.into_iter().collect()
}
