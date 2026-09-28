//! The `petri-analysis/v1` request language and its typed representation.

use std::collections::HashSet;

use pn_core::{ArcDef, Marking, Net, PlaceDef, Token, TransitionDef};
use serde::Deserialize;

use crate::error::{InputError, InputErrorCategory, InputErrorReport};

/// Request language version accepted by this build.
pub const SCHEMA_VERSION: &str = "petri-analysis/v1";

/// Place declared by the client.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaceSpec {
    pub name: String,
    /// Explicit finite capacity. Required by design: this language has no
    /// "unbounded" place, because a bounded model cannot answer unbounded
    /// reachability questions.
    pub capacity: Token,
}

/// Weighted arc endpoint.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArcSpec {
    pub place: String,
    pub weight: Token,
}

/// Transition declared by the client.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransitionSpec {
    pub name: String,
    #[serde(default)]
    pub inputs: Vec<ArcSpec>,
    #[serde(default)]
    pub outputs: Vec<ArcSpec>,
}

/// A reachability target with an optional caller-chosen label echoed back.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NamedTarget {
    #[serde(default)]
    pub label: Option<String>,
    /// Either `by_place` (name -> token count) or a positional `marking`.
    #[serde(default)]
    pub by_place: Option<serde_json::Map<String, serde_json::Value>>,
    #[serde(default)]
    pub marking: Option<Vec<Token>>,
}

/// Toggles and bounds for the analysis run.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct AnalysisOptions {
    /// Maximum number of markings expanded. Absent => full finite box.
    pub max_states: Option<usize>,
    /// Compute P-invariant candidates over the incidence matrix.
    pub compute_invariants: bool,
    /// Enumerate reachable deadlocks.
    pub find_deadlocks: bool,
}

impl Default for AnalysisOptions {
    fn default() -> Self {
        AnalysisOptions {
            max_states: None,
            compute_invariants: true,
            find_deadlocks: true,
        }
    }
}

/// Wire shape of a request (all fields optional here so missing ones become
/// classified SCHEMA errors with a pointer rather than an opaque panic).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestDto {
    #[serde(default)]
    schema: Option<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    places: Option<Vec<PlaceSpec>>,
    #[serde(default)]
    transitions: Option<Vec<TransitionSpec>>,
    #[serde(default)]
    initial: Option<Vec<Token>>,
    #[serde(default)]
    targets: Vec<NamedTarget>,
    #[serde(default)]
    options: Option<AnalysisOptions>,
}

/// Fully typed and validated analysis input, ready for the solver.
#[derive(Debug, Clone)]
pub struct AnalysisInput {
    pub name: String,
    pub net: Net,
    pub targets: Vec<Marking>,
    pub target_labels: Vec<Option<String>>,
    pub options: AnalysisOptions,
}

/// Parse and validate a request body.
pub fn parse_analyze_request(body: &[u8]) -> Result<AnalysisInput, InputErrorReport> {
    // Stage 1: the body must be JSON.
    let value: serde_json::Value = serde_json::from_slice(body).map_err(|e| InputErrorReport {
        errors: vec![InputError {
            category: InputErrorCategory::Syntax,
            message: format!("body is not valid JSON: {e}"),
            pointer: String::new(),
        }],
    })?;

    // Stage 2: the JSON must match the request schema.
    let dto: RequestDto = serde_json::from_value(value).map_err(|e| InputErrorReport {
        errors: vec![InputError {
            category: InputErrorCategory::Schema,
            message: format!("request does not match {SCHEMA_VERSION}: {e}"),
            pointer: serde_path_to_pointer(&e),
        }],
    })?;

    if dto.schema.as_deref() != Some(SCHEMA_VERSION) {
        return Err(InputErrorReport::schema(
            format!(
                "unsupported or missing 'schema'; expected \"{SCHEMA_VERSION}\""
            ),
            "/schema",
        ));
    }

    build_input(dto)
}

fn build_input(dto: RequestDto) -> Result<AnalysisInput, InputErrorReport> {
    let mut errors: Vec<InputError> = Vec::new();

    // Places.
    let place_specs = match dto.places {
        Some(p) if !p.is_empty() => p,
        Some(_) => {
            errors.push(InputError {
                category: InputErrorCategory::Semantic,
                message: "the net must declare at least one place".into(),
                pointer: "/places".into(),
            });
            Vec::new()
        }
        None => {
            return Err(InputErrorReport::schema(
                "missing required field 'places'",
                "/places",
            ));
        }
    };

    // Transitions.
    let transition_specs = match dto.transitions {
        Some(t) => t,
        None => {
            return Err(InputErrorReport::schema(
                "missing required field 'transitions' (an empty array is allowed)",
                "/transitions",
            ));
        }
    };

    // Initial marking.
    let initial = match dto.initial {
        Some(m) => m,
        None => {
            return Err(InputErrorReport::schema(
                "missing required field 'initial'",
                "/initial",
            ));
        }
    };

    // Defensive arc-weight checks (serde would reject negatives; zero is a
    // valid u64 but not a valid arc weight) and dangling references.
    let place_names: HashSet<&str> = place_specs.iter().map(|p| p.name.as_str()).collect();
    for (ti, t) in transition_specs.iter().enumerate() {
        for (ai, a) in t.inputs.iter().enumerate() {
            if a.weight == 0 {
                errors.push(InputError {
                    category: InputErrorCategory::Semantic,
                    message: "input arc weight must be positive, got 0".to_string(),
                    pointer: format!("/transitions/{ti}/inputs/{ai}/weight"),
                });
            }
            if !place_names.contains(a.place.as_str()) {
                errors.push(InputError {
                    category: InputErrorCategory::Semantic,
                    message: format!("input arc references unknown place '{}'", a.place),
                    pointer: format!("/transitions/{ti}/inputs/{ai}/place"),
                });
            }
        }
        for (ai, a) in t.outputs.iter().enumerate() {
            if a.weight == 0 {
                errors.push(InputError {
                    category: InputErrorCategory::Semantic,
                    message: "output arc weight must be positive, got 0".to_string(),
                    pointer: format!("/transitions/{ti}/outputs/{ai}/weight"),
                });
            }
            if !place_names.contains(a.place.as_str()) {
                errors.push(InputError {
                    category: InputErrorCategory::Semantic,
                    message: format!("output arc references unknown place '{}'", a.place),
                    pointer: format!("/transitions/{ti}/outputs/{ai}/place"),
                });
            }
        }
    }

    if !errors.is_empty() {
        return Err(InputErrorReport { errors });
    }

    let places: Vec<PlaceDef> = place_specs
        .iter()
        .map(|p| PlaceDef {
            name: p.name.clone(),
            capacity: p.capacity,
        })
        .collect();
    let transitions: Vec<TransitionDef> = transition_specs
        .iter()
        .map(|t| TransitionDef {
            name: t.name.clone(),
            inputs: t
                .inputs
                .iter()
                .map(|a| ArcDef {
                    place: a.place.clone(),
                    weight: a.weight,
                })
                .collect(),
            outputs: t
                .outputs
                .iter()
                .map(|a| ArcDef {
                    place: a.place.clone(),
                    weight: a.weight,
                })
                .collect(),
        })
        .collect();

    // The kernel performs the authoritative structural validation.
    let net = Net::new(places, transitions, initial).map_err(|e| {
        InputErrorReport::semantic(e.to_string(), semantic_pointer(&e))
    })?;

    // Resolve targets and validate them against capacities/dimensions.
    let mut targets: Vec<Marking> = Vec::with_capacity(dto.targets.len());
    let mut target_labels: Vec<Option<String>> = Vec::with_capacity(dto.targets.len());
    for (i, tgt) in dto.targets.into_iter().enumerate() {
        let resolved = match resolve_target(&net, &tgt) {
            Ok(m) => m,
            Err((msg, sub)) => {
                errors.push(InputError {
                    category: InputErrorCategory::Semantic,
                    message: msg,
                    pointer: format!("/targets/{i}{sub}"),
                });
                continue;
            }
        };
        for (p, &tokens) in resolved.iter().enumerate() {
            if tokens > net.capacity(p) {
                errors.push(InputError {
                    category: InputErrorCategory::Semantic,
                    message: format!(
                        "target has {tokens} tokens on '{}' but capacity is {}",
                        net.place_name(p),
                        net.capacity(p)
                    ),
                    pointer: format!("/targets/{i}"),
                });
            }
        }
        if !errors.is_empty() {
            continue;
        }
        targets.push(resolved);
        target_labels.push(tgt.label);
    }

    if !errors.is_empty() {
        return Err(InputErrorReport { errors });
    }

    Ok(AnalysisInput {
        name: dto.name.unwrap_or_else(|| "unnamed-net".to_string()),
        net,
        targets,
        target_labels,
        options: dto.options.unwrap_or_default(),
    })
}

fn resolve_target(
    net: &Net,
    tgt: &NamedTarget,
) -> Result<Marking, (String, &'static str)> {
    match (&tgt.marking, &tgt.by_place) {
        (Some(_), Some(_)) => Err((
            "target must specify exactly one of 'marking' or 'by_place', got both".into(),
            "",
        )),
        (None, None) => Err((
            "target must specify 'marking' or 'by_place'".into(),
            "",
        )),
        (Some(m), None) => {
            if m.len() != net.place_count() {
                return Err((
                    format!(
                        "target marking length {} does not match place count {}",
                        m.len(),
                        net.place_count()
                    ),
                    "/marking",
                ));
            }
            Ok(m.clone())
        }
        (None, Some(map)) => {
            let mut m = vec![0u64; net.place_count()];
            for (name, val) in map {
                let p = net.place_index(name).ok_or_else(|| {
                    (
                        format!("target references unknown place '{name}'"),
                        "/by_place",
                    )
                })?;
                let tokens = val.as_u64().ok_or_else(|| {
                    (
                        format!("token count for '{name}' must be a non-negative integer"),
                        "/by_place",
                    )
                })?;
                m[p] = tokens;
            }
            Ok(m)
        }
    }
}

fn semantic_pointer(e: &pn_core::CoreError) -> String {
    use pn_core::CoreError::*;
    match e {
        EmptyPlaceSet => "/places".to_string(),
        DuplicatePlace(_) => "/places".to_string(),
        DuplicateTransition(_) => "/transitions".to_string(),
        UnknownPlace { .. } | ZeroWeight { .. } => "/transitions".to_string(),
        DuplicateInput { .. } | DuplicateOutput { .. } => "/transitions".to_string(),
        BadInitialLength { .. } => "/initial".to_string(),
        InitialExceedsCapacity { .. } => "/initial".to_string(),
    }
}

/// Best-effort conversion of a serde_json error path to a JSON pointer.
fn serde_path_to_pointer(e: &serde_json::Error) -> String {
    use serde_json::error::Category;
    if matches!(e.classify(), Category::Syntax) {
        return String::new();
    }
    // serde_json exposes line/column but not a structured path; include the
    // raw line/column so logs and reports can locate the offending token.
    let line = e.line();
    let column = e.column();
    if line == 0 && column == 0 {
        String::new()
    } else {
        format!("@line{line}:col{column}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const VALID: &str = r#"{
        "schema": "petri-analysis/v1",
        "name": "smoke",
        "places": [{"name": "p", "capacity": 3}],
        "transitions": [],
        "initial": [0],
        "targets": [{"marking": [1]}]
    }"#;

    #[test]
    fn parses_valid_request() {
        let input = parse_analyze_request(VALID.as_bytes()).unwrap();
        assert_eq!(input.net.place_count(), 1);
        assert_eq!(input.targets, vec![vec![1]]);
    }

    #[test]
    fn classifies_garbage_as_syntax() {
        let err = parse_analyze_request(b"{not json").unwrap_err();
        assert_eq!(err.primary_category(), InputErrorCategory::Syntax);
    }

    #[test]
    fn classifies_wrong_shape_as_schema() {
        let err = parse_analyze_request(br#"{"schema": "petri-analysis/v1"}"#).unwrap_err();
        assert_eq!(err.primary_category(), InputErrorCategory::Schema);
    }

    #[test]
    fn classifies_unknown_place_arc_as_semantic() {
        let body = br#"{
            "schema": "petri-analysis/v1",
            "places": [{"name": "p", "capacity": 1}],
            "transitions": [{"name": "t", "inputs": [{"place": "ghost", "weight": 1}], "outputs": []}],
            "initial": [0]
        }"#;
        let err = parse_analyze_request(body).unwrap_err();
        assert_eq!(err.primary_category(), InputErrorCategory::Semantic);
        assert!(err.errors[0].pointer.contains("inputs"));
    }

    #[test]
    fn classifies_target_over_capacity_as_semantic() {
        let body = br#"{
            "schema": "petri-analysis/v1",
            "places": [{"name": "p", "capacity": 1}],
            "transitions": [],
            "initial": [0],
            "targets": [{"marking": [2]}]
        }"#;
        let err = parse_analyze_request(body).unwrap_err();
        assert_eq!(err.primary_category(), InputErrorCategory::Semantic);
    }

    #[test]
    fn rejects_bad_schema_version() {
        let err = parse_analyze_request(br#"{"schema": "v0"}"#).unwrap_err();
        assert_eq!(err.primary_category(), InputErrorCategory::Schema);
    }

    #[test]
    fn by_place_target_resolves_in_declaration_order() {
        let body = br#"{
            "schema": "petri-analysis/v1",
            "places": [{"name": "a", "capacity": 2},{"name": "b", "capacity": 2}],
            "transitions": [],
            "initial": [0,0],
            "targets": [{"by_place": {"b": 2}}]
        }"#;
        let input = parse_analyze_request(body).unwrap();
        assert_eq!(input.targets, vec![vec![0, 2]]);
    }
}
