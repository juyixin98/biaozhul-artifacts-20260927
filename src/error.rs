//! 稳定的错误分类。每个变体有固定的机器可读 `code` 与固定 HTTP 状态，
//! 测试按类别断言，而不是匹配字符串。

use std::fmt;

/// 内核错误。变体携带足够的上下文，便于日志复现失败输入。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CoreError {
    /// 请求体不是合法 JSON（或字段缺失/类型不符/含未知字段）。
    BadJson(String),
    /// 坐标列表为空（建表时至少需要一个 x 与一个 y）。
    EmptyCoordinates(&'static str),
    /// 批更新为空。
    EmptyBatch,
    /// 坐标未注册：更新必须只引用建表时预注册的坐标。
    /// 参数为 (x, y)，x 或 y 任一未注册即拒绝，绝不“就近插入”。
    CoordinateNotRegistered { x: i64, y: i64 },
    /// 起点大于终点（inclusive 矩形语义下非法）。
    InvertedRect {
        x_lo: i64,
        x_hi: i64,
        y_lo: i64,
        y_hi: i64,
    },
    /// 版本不存在，或低于 0。
    VersionNotFound { table: u32, version: i64 },
    /// 表不存在，或 id 为非法形式。
    TableNotFound { table: u32 },
    /// 版本已过期：期望基于 `expected_base` 提交，但当前版本已推进到 `current`。
    /// 批更新必须链式追加，旧版本快照永远保留可读，但新提交不能基于旧快照分叉。
    StaleBaseVersion {
        table: u32,
        expected_base: u64,
        current: u64,
    },
    /// 累计值离开 i64 范围。负值允许，只有“累计溢出”才拒绝整批。
    PointOverflow {
        x: i64,
        y: i64,
        previous: i64,
        /// 本批在该点上聚合后的增量（i128，完整可见）。
        batch_delta: i128,
    },
    /// 矩形和离开 i64 范围（树内 i128 完整保留，仅最终结果无法表示）。
    SumOverflow,
    /// 路径参数或请求字段语义非法（如 id 为 0/负/超范围）。
    InvalidRequest(String),
    /// 持久化 I/O 错误（绝不能被包装成成功；对外为 500）。
    Storage(String),
    /// WAL 记录损坏：魔数/长度/CRC/载荷任一校验失败。
    CorruptLog { offset: u64, detail: String },
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CoreError::BadJson(s) => write!(f, "bad_json: {s}"),
            CoreError::EmptyCoordinates(which) => write!(f, "empty coordinate list: {which}"),
            CoreError::EmptyBatch => write!(f, "batch must contain at least one point update"),
            CoreError::CoordinateNotRegistered { x, y } => {
                write!(f, "coordinate ({x}, {y}) is not registered; updates to unregistered coordinates are rejected")
            }
            CoreError::InvertedRect {
                x_lo,
                x_hi,
                y_lo,
                y_hi,
            } => write!(
                f,
                "inverted rectangle: x_lo={x_lo} > x_hi={x_hi} or y_lo={y_lo} > y_hi={y_hi}"
            ),
            CoreError::VersionNotFound { table, version } => {
                write!(f, "table {table} has no version {version}")
            }
            CoreError::TableNotFound { table } => write!(f, "table {table} not found"),
            CoreError::StaleBaseVersion {
                table,
                expected_base,
                current,
            } => write!(
                f,
                "table {table}: expected base version {expected_base}, current version is {current}"
            ),
            CoreError::PointOverflow {
                x,
                y,
                previous,
                batch_delta,
            } => write!(
                f,
                "point ({x}, {y}) overflows i64: previous={previous}, batch_delta={batch_delta}"
            ),
            CoreError::SumOverflow => write!(f, "rectangle sum overflows i64"),
            CoreError::InvalidRequest(s) => write!(f, "invalid request: {s}"),
            CoreError::Storage(s) => write!(f, "storage error: {s}"),
            CoreError::CorruptLog { offset, detail } => {
                write!(f, "corrupt wal record at byte offset {offset}: {detail}")
            }
        }
    }
}

impl std::error::Error for CoreError {}

impl CoreError {
    /// 稳定的机器可读错误码（测试按此断言失败类别）。
    pub fn code(&self) -> &'static str {
        match self {
            CoreError::BadJson(_) => "BAD_JSON",
            CoreError::EmptyCoordinates(_) => "EMPTY_COORDINATES",
            CoreError::EmptyBatch => "EMPTY_BATCH",
            CoreError::CoordinateNotRegistered { .. } => "COORDINATE_NOT_REGISTERED",
            CoreError::InvertedRect { .. } => "INVERTED_RECT",
            CoreError::VersionNotFound { .. } => "VERSION_NOT_FOUND",
            CoreError::TableNotFound { .. } => "TABLE_NOT_FOUND",
            CoreError::StaleBaseVersion { .. } => "STALE_BASE_VERSION",
            CoreError::PointOverflow { .. } => "POINT_OVERFLOW",
            CoreError::SumOverflow => "SUM_OVERFLOW",
            CoreError::InvalidRequest(_) => "INVALID_REQUEST",
            CoreError::Storage(_) => "STORAGE_ERROR",
            CoreError::CorruptLog { .. } => "CORRUPT_LOG",
        }
    }

    /// 固定 HTTP 状态映射。
    pub fn http_status(&self) -> u16 {
        match self {
            CoreError::BadJson(_)
            | CoreError::InvalidRequest(_)
            | CoreError::EmptyCoordinates(_)
            | CoreError::EmptyBatch
            | CoreError::InvertedRect { .. } => 400,
            CoreError::CoordinateNotRegistered { .. }
            | CoreError::PointOverflow { .. }
            | CoreError::SumOverflow => 422,
            CoreError::TableNotFound { .. } | CoreError::VersionNotFound { .. } => 404,
            CoreError::StaleBaseVersion { .. } => 409,
            CoreError::Storage(_) | CoreError::CorruptLog { .. } => 500,
        }
    }
}

pub type CoreResult<T> = Result<T, CoreError>;
