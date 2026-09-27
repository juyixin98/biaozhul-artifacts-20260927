//! JSON input format. The document is first decoded into permissive
//! `serde_json::Value`s and then translated into the same [`RawSystem`] the
//! textual parser produces; all real validation happens in
//! [`crate::eval::build_system`].
//!
//! ```json
//! {
//!   "name": "mutex",
//!   "variables": [
//!     {"name": "x", "type": "bool"},
//!     {"name": "y", "type": "int", "lo": 0, "hi": 3},
//!     {"name": "c", "type": "enum", "variants": ["free", "taken"]}
//!   ],
//!   "init": {"expr": "x == false"},            // or {"state": {"x": false}}
//!   "terminal": ["x == true"],                 // optional, OR-combined
//!   "transitions": [
//!     {"name": "flip", "guard": "true", "assign": [{"target": "x", "expr": "!x"}]}
//!   ]
//! }
//! ```

use serde_json::Value as Json;

use crate::ast::{Assign, Expr, RawSystem, RawTransition};
use crate::error::{BuildError, BuildErrorKind};
use crate::parser::parse_expr;
use crate::system::Domain;

fn je(msg: impl Into<String>) -> BuildError {
    BuildError::new(BuildErrorKind::InvalidJson, msg)
}

fn require_str<'a>(v: &'a Json, field: &str) -> Result<&'a str, BuildError> {
    v.get(field)
        .and_then(|x| x.as_str())
        .ok_or_else(|| je(format!("missing or non-string field '{field}'")))
}

pub fn system_from_json(doc: &Json) -> Result<RawSystem, BuildError> {
    let obj = doc
        .as_object()
        .ok_or_else(|| je("top level must be an object"))?;

    let name = obj.get("name").and_then(|v| v.as_str()).map(String::from);

    let vars_json = obj
        .get("variables")
        .and_then(|v| v.as_array())
        .ok_or_else(|| je("'variables' must be an array"))?;

    let mut vars = Vec::new();
    for v in vars_json {
        let vname = require_str(v, "name")?.to_string();
        let ty = require_str(v, "type")?;
        let domain = match ty {
            "bool" => Domain::Bool,
            "int" => {
                let lo = v
                    .get("lo")
                    .and_then(|x| x.as_i64())
                    .ok_or_else(|| je("int variable needs integer 'lo'"))?;
                let hi = v
                    .get("hi")
                    .and_then(|x| x.as_i64())
                    .ok_or_else(|| je("int variable needs integer 'hi'"))?;
                Domain::IntRange { lo, hi }
            }
            "enum" => {
                let variants = v
                    .get("variants")
                    .and_then(|x| x.as_array())
                    .ok_or_else(|| je("enum variable needs a 'variants' array"))?
                    .iter()
                    .map(|x| {
                        x.as_str()
                            .map(String::from)
                            .ok_or_else(|| je("enum variant must be a string"))
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                Domain::Enum { variants }
            }
            other => {
                return Err(je(format!("unknown variable type '{other}'")));
            }
        };
        vars.push((vname, domain));
    }

    let init_json = obj.get("init").ok_or_else(|| je("missing 'init'"))?;
    let (init_predicate, init_state) = if let Some(s) = init_json.get("expr") {
        let s = s
            .as_str()
            .ok_or_else(|| je("'init.expr' must be a string"))?;
        (Some(parse_expr(s)?), Vec::new())
    } else if let Some(st) = init_json.get("state") {
        let map = st
            .as_object()
            .ok_or_else(|| je("'init.state' must be an object"))?;
        let mut pairs = Vec::new();
        for (k, val) in map {
            pairs.push((k.clone(), json_literal(val)?));
        }
        (None, pairs)
    } else {
        return Err(je("'init' must contain either 'expr' or 'state'"));
    };

    let mut terminals = Vec::new();
    if let Some(t) = obj.get("terminal") {
        match t {
            Json::String(s) => terminals.push(parse_expr(s)?),
            Json::Array(arr) => {
                for a in arr {
                    let s = a
                        .as_str()
                        .ok_or_else(|| je("terminal entries must be strings"))?;
                    terminals.push(parse_expr(s)?);
                }
            }
            _ => return Err(je("'terminal' must be a string or array of strings")),
        }
    }

    let trans_json = obj
        .get("transitions")
        .and_then(|v| v.as_array())
        .ok_or_else(|| je("'transitions' must be an array"))?;
    let mut transitions = Vec::new();
    for t in trans_json {
        let tname = require_str(t, "name")?.to_string();
        let guard = parse_expr(require_str(t, "guard")?)?;
        let assign_json = t
            .get("assign")
            .and_then(|v| v.as_array())
            .ok_or_else(|| je(format!("transition '{tname}' needs an 'assign' array")))?;
        let mut assign = Vec::new();
        for a in assign_json {
            let target = require_str(a, "target")?.to_string();
            let rhs = if let Some(s) = a.get("expr").and_then(|x| x.as_str()) {
                parse_expr(s)?
            } else if let Some(v) = a.get("value") {
                json_literal(v)?
            } else {
                return Err(je("assignment needs 'expr' or 'value'"));
            };
            assign.push(Assign {
                target,
                target_index: None,
                rhs,
            });
        }
        transitions.push(RawTransition {
            name: tname,
            guard,
            assign,
        });
    }

    Ok(RawSystem {
        name,
        vars,
        init_predicate,
        init_state,
        transitions,
        terminals,
    })
}

/// Turn a JSON scalar into a literal [`Expr`]. Boolean/int values only;
/// strings are interpreted as enum-variant names.
fn json_literal(v: &Json) -> Result<Expr, BuildError> {
    match v {
        Json::Bool(b) => Ok(Expr::Bool(*b)),
        Json::Number(n) => n
            .as_i64()
            .map(Expr::Int)
            .ok_or_else(|| je("numeric values must be i64 integers")),
        Json::String(s) => Ok(Expr::Name {
            name: s.clone(),
            res: None,
        }),
        _ => Err(je(
            "state values must be boolean, integer or enum-name strings",
        )),
    }
}
