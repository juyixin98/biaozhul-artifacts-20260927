//! JSON 网描述（serde DTO，`deny_unknown_fields` 以尽早暴露拼写错误）。

use serde::Deserialize;

use super::{codes, build_net, InputError, NetSpec, PlaceSpec, TransitionSpec};
use crate::kernel::model::Net;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NetDto {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub places: Vec<PlaceDto>,
    #[serde(default)]
    pub transitions: Vec<TransitionDto>,
    pub initial_marking: Option<Vec<i64>>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaceDto {
    pub name: String,
    pub capacity: i64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransitionDto {
    pub name: String,
    #[serde(default)]
    pub inputs: Vec<ArcDto>,
    #[serde(default)]
    pub outputs: Vec<ArcDto>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArcDto {
    pub place: String,
    #[serde(default = "default_weight")]
    pub weight: i64,
}

fn default_weight() -> i64 {
    1
}

/// 解析 JSON 网描述并完成语义校验。
pub fn parse_json_net(text: &str) -> Result<(Net, Option<Vec<i64>>), InputError> {
    let dto: NetDto = serde_json::from_str(text).map_err(|e| {
        InputError::one(
            codes::PARSE_ERROR,
            format!("invalid JSON net: {e}"),
            None,
        )
    })?;
    let spec = NetSpec {
        places: dto
            .places
            .into_iter()
            .map(|p| PlaceSpec {
                name: p.name,
                capacity: p.capacity,
            })
            .collect(),
        transitions: dto
            .transitions
            .into_iter()
            .map(|t| TransitionSpec {
                name: t.name,
                inputs: t
                    .inputs
                    .into_iter()
                    .map(|a| (a.place, a.weight))
                    .collect(),
                outputs: t
                    .outputs
                    .into_iter()
                    .map(|a| (a.place, a.weight))
                    .collect(),
            })
            .collect(),
        initial_marking: dto.initial_marking,
    };
    let initial = spec.initial_marking.clone();
    let net = build_net(&spec)?;
    Ok((net, initial))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::input::codes;

    #[test]
    fn parses_minimal_weighted_net() {
        let text = r#"{
            "places": [{"name":"p","capacity":2}],
            "transitions": [{"name":"t","outputs":[{"place":"p","weight":1}]}],
            "initial_marking": [0]
        }"#;
        let (net, init) = parse_json_net(text).unwrap();
        assert_eq!(net.place_count(), 1);
        assert_eq!(net.transition_count(), 1);
        assert_eq!(init.unwrap(), vec![0]);
    }

    #[test]
    fn rejects_each_failure_category() {
        let cases: &[(&str, &str)] = &[
            (r#"{"places":[],"transitions":[]}"#, codes::NET_EMPTY),
            (
                r#"{"places":[{"name":"p","capacity":1},{"name":"p","capacity":1}],"transitions":[]}"#,
                codes::DUPLICATE_PLACE,
            ),
            (
                r#"{"places":[{"name":"p","capacity":-1}],"transitions":[]}"#,
                codes::NEGATIVE_CAPACITY,
            ),
            (
                r#"{"places":[{"name":"p","capacity":1}],"transitions":[{"name":"t","inputs":[{"place":"p","weight":0}]}]}"#,
                codes::NONPOSITIVE_WEIGHT,
            ),
            (
                r#"{"places":[{"name":"p","capacity":1}],"transitions":[{"name":"t","inputs":[{"place":"q","weight":1}]}]}"#,
                codes::UNKNOWN_PLACE,
            ),
            (
                r#"{"places":[{"name":"p","capacity":1}],"transitions":[],"initial_marking":[2]}"#,
                codes::TOKEN_EXCEEDS_CAPACITY,
            ),
            (
                r#"{"places":[{"name":"p","capacity":1}],"transitions":[],"initial_marking":[1,0]}"#,
                codes::MARKING_LENGTH,
            ),
            (r#"{not json"#, codes::PARSE_ERROR),
            (
                r#"{"places":[{"name":"p","capacity":1,"extra":2}],"transitions":[]}"#,
                codes::PARSE_ERROR, // deny_unknown_fields
            ),
        ];
        for (text, expected_code) in cases {
            let err = parse_json_net(text).expect_err("input must be rejected");
            assert_eq!(
                err.primary_code(),
                *expected_code,
                "input {text}: expected code {expected_code}, got {:?}",
                err.issues
            );
        }
    }

    #[test]
    fn collects_multiple_issues_in_one_pass() {
        let text = r#"{"places":[{"name":"p","capacity":-2},{"name":"p","capacity":1}],"transitions":[]}"#;
        let err = parse_json_net(text).expect_err("must reject");
        let codes_seen: Vec<&str> = err.issues.iter().map(|i| i.code).collect();
        assert!(codes_seen.contains(&codes::NEGATIVE_CAPACITY));
        assert!(codes_seen.contains(&codes::DUPLICATE_PLACE));
    }
}
