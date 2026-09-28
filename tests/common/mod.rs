//! 测试公共设施。
//!
//! - [`oracle::Oracle`]：**独立于被测内核**的参考实现——
//!   `HashMap<(i64,i64), i128>` 稀疏映射 + 矩形全扫描，逐版本保存点值快照。
//!   它不调用任何 pr2d 内核代码，期望值由它从头手算（另有手算常量在各测试中）。
//! - [`TestLog`]：把每个断言的输入、版本、计算步骤、判定依据写入按运行身份
//!   组织的 JSONL 日志，复现失败时可关联运行/请求。
//! - [`TempDir`]：std-only 临时目录；设 `PR2D_KEEP_TMP=1` 可保留现场。

// 每个集成测试二进制独立编译 common；未用到的设施在某个二进制里“死代码”是正常的。
#![allow(dead_code)]

pub mod oracle;
pub mod temp;
pub mod testlog;

#[allow(unused_imports)]
pub use oracle::{Oracle, OracleRect};
pub use temp::TempDir;
pub use testlog::TestLog;
