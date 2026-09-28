//! HTTP 层请求/响应 DTO。所有入参 DTO 使用 `deny_unknown_fields`，
//! 多余/拼错字段一律 `BAD_JSON`（400），不会被静默忽略。

use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegisterReq {
    /// 建表用的原始 x 坐标（可含重复，服务端排序去重并报告数量）。
    pub xs: Vec<i64>,
    pub ys: Vec<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BatchReq {
    #[serde(default)]
    pub base_version: Option<i64>,
    pub updates: Vec<crate::model::PointUpdate>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QueryReq {
    /// 不填则查当前最新版本；可填历史版本号（含基线 0）。
    #[serde(default)]
    pub version: Option<i64>,
    #[serde(flatten)]
    pub rect: crate::rect::Rect,
}

/// 统一错误信封：失败绝不返回 200，且携带运行/请求身份。
#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub ok: bool,
    pub error: ErrorDetail,
}

#[derive(Debug, Serialize)]
pub struct ErrorDetail {
    pub code: String,
    pub message: String,
    pub request_id: String,
    pub run_id: String,
}
