//! 求解预算：墙钟时限与传播/决策步数上限。
//!
//! 预算耗尽是**可中断**条件，结论必须是 UNKNOWN，而不是 UNSAT。
//! 检查点设在：每次传播外层循环、每次决策、每次冲突分析之后。

use std::time::{Duration, Instant};

#[derive(Debug, Clone)]
pub struct Budget {
    /// 墙钟上限；None 表示不限时。
    pub time_limit: Option<Duration>,
    /// 外层 BCP 循环（处理一个被监视文字的触发）次数上限。
    pub max_propagations: Option<u64>,
    /// 决策次数上限。
    pub max_decisions: Option<u64>,
}

impl Default for Budget {
    fn default() -> Self {
        Budget {
            time_limit: Some(Duration::from_secs(10)),
            max_propagations: Some(10_000_000),
            max_decisions: Some(1_000_000),
        }
    }
}

impl Budget {
    pub fn unlimited() -> Self {
        Budget {
            time_limit: None,
            max_propagations: None,
            max_decisions: None,
        }
    }
}

/// 预算耗尽的具体原因（进入 UNKNOWN 的诊断）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BudgetExceeded {
    TimeLimit,
    PropagationLimit(u64),
    DecisionLimit(u64),
}

#[derive(Debug, Clone, Default)]
pub struct Counters {
    pub propagations: u64,
    pub decisions: u64,
    pub conflicts: u64,
    pub learned_clauses: u64,
}

pub(crate) struct BudgetGuard {
    start: Instant,
}

impl BudgetGuard {
    pub fn start() -> Self {
        BudgetGuard {
            start: Instant::now(),
        }
    }

    pub fn check(
        &self,
        budget: &Budget,
        counters: &Counters,
    ) -> Result<(), BudgetExceeded> {
        if let Some(t) = budget.time_limit {
            if self.start.elapsed() >= t {
                return Err(BudgetExceeded::TimeLimit);
            }
        }
        if let Some(m) = budget.max_propagations {
            if counters.propagations >= m {
                return Err(BudgetExceeded::PropagationLimit(counters.propagations));
            }
        }
        if let Some(m) = budget.max_decisions {
            if counters.decisions >= m {
                return Err(BudgetExceeded::DecisionLimit(counters.decisions));
            }
        }
        Ok(())
    }
}
