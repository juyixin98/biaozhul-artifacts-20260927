//! HTTP request/response types and the build/run adapter shared by HTTP and
//! CLI entry points.

use serde::{Deserialize, Serialize};

use fsm_core::{CheckOptions, Query, QueryKind};
use fsm_lang::ast::RawSystem;
use fsm_lang::parser::{parse_expr, parse_system};
use fsm_lang::{eval::build_system, System};

/// One property in a request.
#[derive(Debug, Deserialize)]
pub struct PropertyInput {
    pub name: String,
    /// "ag" | "ef"
    pub kind: String,
    #[serde(default)]
    pub expr: Option<String>,
}

/// Check request payload. Exactly one of `spec_text` / `spec_json` must be
/// present; alternatively `fixture` selects a built-in local fixture.
#[derive(Debug, Deserialize)]
pub struct CheckRequest {
    #[serde(default)]
    pub fixture: Option<String>,
    #[serde(default)]
    pub spec_text: Option<String>,
    #[serde(default)]
    pub spec_json: Option<serde_json::Value>,
    #[serde(default)]
    pub properties: Vec<PropertyInput>,
    #[serde(default)]
    pub max_states: Option<u64>,
    /// Run the implicit deadlock check in addition to supplied properties.
    #[serde(default)]
    pub check_deadlock: Option<bool>,
}

#[derive(Debug, Serialize)]
pub struct BuildFailure {
    pub category: String,
    pub detail: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub position: Option<(usize, usize)>,
}

/// Everything needed to run a check, resolved from a [`CheckRequest`].
pub struct ResolvedCheck {
    pub system: System,
    pub queries: Vec<Query>,
    pub options: CheckOptions,
}

/// Resolve the specification source and parse/type-check it.
pub fn resolve_spec(req: &CheckRequest) -> Result<RawSystem, BuildFailure> {
    let sources = [
        req.fixture.is_some(),
        req.spec_text.is_some(),
        req.spec_json.is_some(),
    ];
    if sources.iter().filter(|x| **x).count() != 1 {
        return Err(BuildFailure {
            category: "invalid_request".into(),
            detail: "exactly one of fixture/spec_text/spec_json must be provided".into(),
            position: None,
        });
    }
    if let Some(name) = &req.fixture {
        let text = match name.as_str() {
            "mutex_safe" => fsm_fixtures::mutex_safe(),
            "mutex_bad" => fsm_fixtures::mutex_bad(),
            "counter" => fsm_fixtures::counter(),
            "counter_deadlock" => fsm_fixtures::counter_deadlock(),
            "no_init" => fsm_fixtures::no_init(),
            "big_counter" => fsm_fixtures::big_counter(),
            "swap" => fsm_fixtures::swap(),
            other => {
                return Err(BuildFailure {
                    category: "unknown_fixture".into(),
                    detail: format!("no built-in fixture named '{other}'"),
                    position: None,
                })
            }
        };
        return parse_system(text).map_err(build_failure);
    }
    if let Some(text) = &req.spec_text {
        parse_system(text).map_err(build_failure)
    } else if let Some(json) = &req.spec_json {
        let raw = fsm_lang::json::system_from_json(json).map_err(build_failure)?;
        Ok(raw)
    } else {
        unreachable!()
    }
}

pub fn build_failure(e: fsm_lang::BuildError) -> BuildFailure {
    // BuildErrorKind carries a serde rename_all = "snake_case" representation;
    // serialize it so the category is stable (e.g. "unknown_name", not the
    // Debug spelling "UnknownName" -> "unknownname").
    let category = serde_json::to_value(e.kind)
        .ok()
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_else(|| "build_error".into());
    BuildFailure {
        category,
        detail: e.message,
        position: e.position,
    }
}

/// Build and prepare queries.
pub fn prepare(req: CheckRequest, default_budget: u64) -> Result<ResolvedCheck, BuildFailure> {
    let raw = resolve_spec(&req)?;
    let system = build_system(raw).map_err(build_failure)?;

    let mut queries: Vec<Query> = Vec::new();
    for p in &req.properties {
        let kind = match p.kind.as_str() {
            "ag" => QueryKind::Ag,
            "ef" => QueryKind::Ef,
            other => {
                return Err(BuildFailure {
                    category: "invalid_request".into(),
                    detail: format!("property kind '{other}' must be 'ag' or 'ef'"),
                    position: None,
                })
            }
        };
        let src = p.expr.clone().ok_or_else(|| BuildFailure {
            category: "invalid_request".into(),
            detail: format!("property '{}' needs an expression", p.name),
            position: None,
        })?;
        let mut expr = parse_expr(&src).map_err(build_failure)?;
        fsm_lang::eval::check_boolean_predicate(&system, &mut expr).map_err(build_failure)?;
        queries.push(Query {
            name: p.name.clone(),
            kind,
            predicate: Some(expr),
            source: src,
        });
    }
    if req.check_deadlock.unwrap_or(true) {
        queries.push(Query {
            name: "implicit_deadlock_freedom".into(),
            kind: QueryKind::DeadlockFree,
            predicate: None,
            source: String::new(),
        });
    }
    let options = CheckOptions {
        max_states: req.max_states.unwrap_or(default_budget),
    };
    Ok(ResolvedCheck {
        system,
        queries,
        options,
    })
}
