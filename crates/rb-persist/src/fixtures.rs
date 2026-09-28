//! JSON 夹具导入/导出：最简单的 `[1, 2, 3, ...]` 整数数组格式。
//!
//! 该格式不是集合的存储格式（存储用 [`rb_format::codec`] 的二进制），
//! 仅用于人与外部工具（含 Python 参考实现）交换夹具数据。

use std::collections::BTreeSet;
use std::io;

use rb_format::RoaringSet;

/// 夹具解析错误。
#[derive(Debug)]
pub enum FixtureError {
    /// JSON 语法错误（附带位置/原因摘要）。
    Json(String),
    /// 数组中出现非 u32 可表示的值（负数或超过 u32::MAX）。
    OutOfRange(i64),
    /// 顶层不是 JSON 数组。
    NotAnArray,
    Io(io::Error),
}

impl std::fmt::Display for FixtureError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FixtureError::Json(m) => write!(f, "invalid fixture JSON: {m}"),
            FixtureError::OutOfRange(v) => write!(f, "value {v} outside u32 range"),
            FixtureError::NotAnArray => write!(f, "fixture must be a JSON array of integers"),
            FixtureError::Io(e) => write!(f, "i/o error: {e}"),
        }
    }
}

impl std::error::Error for FixtureError {}

impl From<io::Error> for FixtureError {
    fn from(e: io::Error) -> Self {
        FixtureError::Io(e)
    }
}

impl From<serde_json::Error> for FixtureError {
    fn from(e: serde_json::Error) -> Self {
        FixtureError::Json(e.to_string())
    }
}

/// 解析 JSON 整数数组为集合（自动去重，顺序无关）。
pub fn parse_json(json: &[u8]) -> Result<RoaringSet, FixtureError> {
    let raw: serde_json::Value = serde_json::from_slice(json)?;
    let arr = raw.as_array().ok_or(FixtureError::NotAnArray)?;
    let mut set = RoaringSet::new();
    for v in arr {
        if let Some(n) = v.as_u64() {
            if n > u32::MAX as u64 {
                return Err(FixtureError::OutOfRange(n as i64));
            }
            set.insert(n as u32);
        } else if let Some(neg) = v.as_i64() {
            return Err(FixtureError::OutOfRange(neg));
        } else {
            return Err(FixtureError::NotAnArray);
        }
    }
    Ok(set)
}

/// 把集合导出为排序、紧凑的 JSON 整数数组（稳定字节，便于 diff）。
pub fn to_json(set: &RoaringSet) -> Vec<u8> {
    // 先用 BTreeSet 得到严格排序输出（集合自身 iter 已升序，这里显式表达意图）。
    let ordered: BTreeSet<u32> = set.iter().collect();
    let mut out = String::from("[");
    for (i, v) in ordered.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&v.to_string());
    }
    out.push(']');
    out.into_bytes()
}
