//! # cuckoo-verify
//!
//! 独立验证器：自带 XXH64 参考实现（[`xxh64`]）、固定夹具（[`oracle`]）、
//! 独立迁移参考模型（[`model`]），并以场景形式对被测的 `cuckoo-core` /
//! `cuckoo-persist` 做**具体值与具体失败类别**的断言。
//!
//! 所有输出带运行身份（run id）、版本、时间戳、步骤与判定依据，
//! 便于把日志与某次输入运行关联起来。

pub mod harness;
pub mod model;
pub mod oracle;
pub mod scenarios;
pub mod xxh64;
