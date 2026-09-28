//! 内核数据模型：点增量与版本元信息。

use serde::{Deserialize, Serialize};

/// 一次点增量：`(x, y)` 坐标加上 `delta`（允许负值）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PointUpdate {
    pub x: i64,
    pub y: i64,
    pub delta: i64,
}

/// 一个已发布版本的元信息。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct VersionInfo {
    /// 表内版本号，基线为 0，每批原子发布后 +1。
    pub version: u64,
    /// 该版本含多少个点增量（去重聚合前的原始条目数）。
    pub update_count: usize,
    /// 发布时间（Unix 毫秒）。
    pub created_at_ms: i64,
    /// 该版本基于的父版本（基线版本 0 的 base 为 0）。
    pub base_version: u64,
}

impl VersionInfo {
    pub fn baseline(now_ms: i64) -> VersionInfo {
        VersionInfo {
            version: 0,
            update_count: 0,
            created_at_ms: now_ms,
            base_version: 0,
        }
    }
}
