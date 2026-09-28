//! 输入语言层：JSON 网描述与 `.pnet` 文本语言，统一解析为 [`crate::kernel::Net`]。
//!
//! 两种格式都经过同一条语义校验流水线（拒绝重复名、未知引用、0 权弧、
//! 非法容量、超界数值等），保证内核收到的网结构合法。

pub mod json;
pub mod pnet;

pub use json::parse_json_net;
pub use pnet::parse_pnet;

use crate::kernel::model::{ArcExpr, Marking, Net, Place, Transition};

/// 校验问题类别（测试按稳定代码断言“失败类别”）。
pub mod codes {
    pub const PARSE_ERROR: &str = "parse_error";
    pub const PLACE_COUNT: &str = "place_count";
    pub const PLACE_EMPTY_NAME: &str = "place_empty_name";
    pub const DUPLICATE_PLACE: &str = "duplicate_place";
    pub const TRANSITION_EMPTY_NAME: &str = "transition_empty_name";
    pub const DUPLICATE_TRANSITION: &str = "duplicate_transition";
    pub const UNKNOWN_PLACE: &str = "unknown_place";
    pub const DUPLICATE_ARC: &str = "duplicate_arc";
    pub const NONPOSITIVE_WEIGHT: &str = "nonpositive_weight";
    pub const WEIGHT_TOO_LARGE: &str = "weight_too_large";
    pub const NEGATIVE_CAPACITY: &str = "negative_capacity";
    pub const CAPACITY_TOO_LARGE: &str = "capacity_too_large";
    pub const NEGATIVE_TOKENS: &str = "negative_tokens";
    pub const TOKEN_EXCEEDS_CAPACITY: &str = "token_exceeds_capacity";
    pub const MARKING_LENGTH: &str = "marking_length";
    pub const BAD_NUMBER: &str = "bad_number";
    pub const NET_EMPTY: &str = "net_empty";
    pub const VALUE_OUT_OF_RANGE: &str = "value_out_of_range";
}

/// 单条诊断：稳定错误码 + 人类可读消息 + 源位置（文本格式）或 JSON 指针。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Issue {
    pub code: &'static str,
    pub message: String,
    pub location: Option<String>,
}

#[derive(Debug, Clone, thiserror::Error)]
#[error("{issues:?}")]
pub struct InputError {
    pub issues: Vec<Issue>,
}

impl InputError {
    pub fn one(code: &'static str, message: impl Into<String>, location: Option<String>) -> Self {
        InputError {
            issues: vec![Issue {
                code,
                message: message.into(),
                location,
            }],
        }
    }

    /// 主错误码（响应里的 error 字段）。
    pub fn primary_code(&self) -> &'static str {
        self.issues.first().map(|i| i.code).unwrap_or(codes::PARSE_ERROR)
    }
}

/// 数值安全上界：留出充足的整数运算空间，杜绝内部溢出。
pub const MAX_VALUE: i64 = i64::MAX / 4;

/// 两种格式解析后的中间表示，随后统一走 [`build_net`] 校验与构造。
#[derive(Debug, Clone)]
pub struct NetSpec {
    pub places: Vec<PlaceSpec>,
    pub transitions: Vec<TransitionSpec>,
    pub initial_marking: Option<Vec<i64>>,
}

#[derive(Debug, Clone)]
pub struct PlaceSpec {
    pub name: String,
    pub capacity: i64,
}

#[derive(Debug, Clone, Default)]
pub struct TransitionSpec {
    pub name: String,
    /// (库所名, 权重)
    pub inputs: Vec<(String, i64)>,
    pub outputs: Vec<(String, i64)>,
}

/// 统一语义校验与内核对象构造。
pub fn build_net(spec: &NetSpec) -> Result<Net, InputError> {
    let mut issues: Vec<Issue> = Vec::new();

    if spec.places.is_empty() {
        issues.push(Issue {
            code: codes::NET_EMPTY,
            message: "net must declare at least one place".into(),
            location: None,
        });
    }

    // 库所：名称、重复、容量。
    let mut seen_places = std::collections::HashSet::new();
    for (i, p) in spec.places.iter().enumerate() {
        if p.name.is_empty() {
            issues.push(Issue {
                code: codes::PLACE_EMPTY_NAME,
                message: format!("place at index {i} has an empty name"),
                location: Some(format!("/places/{i}")),
            });
        } else if !seen_places.insert(p.name.clone()) {
            issues.push(Issue {
                code: codes::DUPLICATE_PLACE,
                message: format!("duplicate place name '{}'", p.name),
                location: Some(format!("/places/{i}")),
            });
        }
        if p.capacity < 0 {
            issues.push(Issue {
                code: codes::NEGATIVE_CAPACITY,
                message: format!("place '{}' has negative capacity {}", p.name, p.capacity),
                location: Some(format!("/places/{i}/capacity")),
            });
        } else if p.capacity > MAX_VALUE {
            issues.push(Issue {
                code: codes::CAPACITY_TOO_LARGE,
                message: format!(
                    "place '{}' capacity {} exceeds safety bound {MAX_VALUE}",
                    p.name, p.capacity
                ),
                location: Some(format!("/places/{i}/capacity")),
            });
        }
    }

    // 变迁：名称、重复。
    let mut seen_transitions = std::collections::HashSet::new();
    for (i, t) in spec.transitions.iter().enumerate() {
        if t.name.is_empty() {
            issues.push(Issue {
                code: codes::TRANSITION_EMPTY_NAME,
                message: format!("transition at index {i} has an empty name"),
                location: Some(format!("/transitions/{i}")),
            });
        } else if !seen_transitions.insert(t.name.clone()) {
            issues.push(Issue {
                code: codes::DUPLICATE_TRANSITION,
                message: format!("duplicate transition name '{}'", t.name),
                location: Some(format!("/transitions/{i}")),
            });
        }
    }

    // 名称 -> 索引。
    let place_index: std::collections::HashMap<&str, usize> = spec
        .places
        .iter()
        .enumerate()
        .map(|(i, p)| (p.name.as_str(), i))
        .collect();

    // 弧：未知库所、重复弧、权重。
    for (ti, t) in spec.transitions.iter().enumerate() {
        let mut in_seen = std::collections::HashSet::new();
        for (k, (pname, w)) in t.inputs.iter().enumerate() {
            let loc = format!("/transitions/{ti}/inputs/{k}");
            let Some(&pidx) = place_index.get(pname.as_str()) else {
                issues.push(Issue {
                    code: codes::UNKNOWN_PLACE,
                    message: format!("input arc references unknown place '{pname}'"),
                    location: Some(loc.clone()),
                });
                continue;
            };
            if !in_seen.insert(pidx) {
                issues.push(Issue {
                    code: codes::DUPLICATE_ARC,
                    message: format!("duplicate input arc transition '{}' -> place '{pname}'", t.name),
                    location: Some(loc.clone()),
                });
            }
            check_weight(*w, pname, &loc, &mut issues);
        }
        let mut out_seen = std::collections::HashSet::new();
        for (k, (pname, w)) in t.outputs.iter().enumerate() {
            let loc = format!("/transitions/{ti}/outputs/{k}");
            let Some(&pidx) = place_index.get(pname.as_str()) else {
                issues.push(Issue {
                    code: codes::UNKNOWN_PLACE,
                    message: format!("output arc references unknown place '{pname}'"),
                    location: Some(loc.clone()),
                });
                continue;
            };
            if !out_seen.insert(pidx) {
                issues.push(Issue {
                    code: codes::DUPLICATE_ARC,
                    message: format!("duplicate output arc transition '{}' -> place '{pname}'", t.name),
                    location: Some(loc.clone()),
                });
            }
            check_weight(*w, pname, &loc, &mut issues);
        }
    }

    if let Some(marking) = &spec.initial_marking {
        if marking.len() != spec.places.len() {
            issues.push(Issue {
                code: codes::MARKING_LENGTH,
                message: format!(
                    "initial marking has {} entries but net has {} places",
                    marking.len(),
                    spec.places.len()
                ),
                location: Some("/initial_marking".into()),
            });
        }
        for (i, &tok) in marking.iter().enumerate() {
            if tok < 0 {
                issues.push(Issue {
                    code: codes::NEGATIVE_TOKENS,
                    message: format!("initial marking token count {tok} at place index {i} is negative"),
                    location: Some(format!("/initial_marking/{i}")),
                });
            } else if let Some(p) = spec.places.get(i) {
                if tok > p.capacity {
                    issues.push(Issue {
                        code: codes::TOKEN_EXCEEDS_CAPACITY,
                        message: format!(
                            "initial marking {} exceeds capacity {} of place '{}'",
                            tok, p.capacity, p.name
                        ),
                        location: Some(format!("/initial_marking/{i}")),
                    });
                }
            }
        }
    }

    if !issues.is_empty() {
        return Err(InputError { issues });
    }

    // 构造内核对象（零变迁的网合法；输入弧按库所索引排序以保持确定性）。
    let places = spec
        .places
        .iter()
        .map(|p| Place {
            name: p.name.clone(),
            capacity: p.capacity,
        })
        .collect();
    let transitions = spec
        .transitions
        .iter()
        .map(|t| {
            let mut inputs: Vec<ArcExpr> = t
                .inputs
                .iter()
                .map(|(pname, w)| ArcExpr {
                    place: place_index[pname.as_str()],
                    weight: *w,
                })
                .collect();
            inputs.sort_by_key(|a| a.place);
            let mut outputs: Vec<ArcExpr> = t
                .outputs
                .iter()
                .map(|(pname, w)| ArcExpr {
                    place: place_index[pname.as_str()],
                    weight: *w,
                })
                .collect();
            outputs.sort_by_key(|a| a.place);
            Transition {
                name: t.name.clone(),
                inputs,
                outputs,
            }
        })
        .collect();

    Ok(Net {
        places,
        transitions,
    })
}

fn check_weight(w: i64, place: &str, loc: &str, issues: &mut Vec<Issue>) {
    if w <= 0 {
        issues.push(Issue {
            code: codes::NONPOSITIVE_WEIGHT,
            message: format!("arc weight {w} on place '{place}' must be a positive integer (omit the arc or drop the arc entirely for zero flow)"),
            location: Some(loc.into()),
        });
    } else if w > MAX_VALUE {
        issues.push(Issue {
            code: codes::WEIGHT_TOO_LARGE,
            message: format!("arc weight {w} on place '{place}' exceeds safety bound {MAX_VALUE}"),
            location: Some(loc.into()),
        });
    }
}

/// 校验目标/证据标识：长度、非负、容量边界。
pub fn validate_marking(net: &Net, marking: &[i64], field: &str) -> Result<Marking, InputError> {
    let mut issues = Vec::new();
    if marking.len() != net.place_count() {
        issues.push(Issue {
            code: codes::MARKING_LENGTH,
            message: format!(
                "{field} has {} entries but net has {} places",
                marking.len(),
                net.place_count()
            ),
            location: Some(format!("/{field}")),
        });
        return Err(InputError { issues });
    }
    for (i, &tok) in marking.iter().enumerate() {
        if tok < 0 {
            issues.push(Issue {
                code: codes::NEGATIVE_TOKENS,
                message: format!("{field} token count {tok} at place '{}' is negative", net.places[i].name),
                location: Some(format!("/{field}/{i}")),
            });
        } else if tok > net.places[i].capacity {
            issues.push(Issue {
                code: codes::TOKEN_EXCEEDS_CAPACITY,
                message: format!(
                    "{field} token count {tok} exceeds capacity {} of place '{}'",
                    net.places[i].capacity, net.places[i].name
                ),
                location: Some(format!("/{field}/{i}")),
            });
        }
    }
    if issues.is_empty() {
        Ok(Marking(marking.to_vec()))
    } else {
        Err(InputError { issues })
    }
}
