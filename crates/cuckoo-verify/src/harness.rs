//! 验证日志与判定框架：每条记录都带运行身份、版本、场景、步骤与判定依据。

use std::fmt;
use std::time::{SystemTime, UNIX_EPOCH};

/// 判定结论。`Fail` 必须带“为什么”以及观察值/期望值，禁止笼统失败。
#[derive(Debug, Clone)]
pub enum Verdict {
    Pass,
    Fail {
        reason: String,
        observed: String,
        expected: String,
    },
}

impl Verdict {
    pub fn is_pass(&self) -> bool {
        matches!(self, Verdict::Pass)
    }
}

#[derive(Debug, Clone)]
pub struct StepRecord {
    pub seq: usize,
    pub name: String,
    pub detail: String,
    pub verdict: Verdict,
}

#[derive(Debug, Clone)]
pub struct ScenarioReport {
    pub id: &'static str,
    pub title: &'static str,
    pub run_id: String,
    pub core_version: &'static str,
    pub format_version: u32,
    pub started_unix_ms: u128,
    pub finished_unix_ms: Option<u128>,
    pub steps: Vec<StepRecord>,
}

impl ScenarioReport {
    pub fn passed(&self) -> bool {
        self.steps.iter().all(|s| s.verdict.is_pass())
    }

    /// 面向终端的逐行日志。
    pub fn render_text(&self) -> String {
        let mut out = String::new();
        out.push_str(&"=".repeat(78));
        out.push('\n');
        out.push_str(&format!(
            "场景 [{id}] {title}\n运行 {run}  内核版本 {cv}  格式版本 v{fv}  开始 {ts}\n",
            id = self.id,
            title = self.title,
            run = self.run_id,
            cv = self.core_version,
            fv = self.format_version,
            ts = self.started_unix_ms
        ));
        out.push_str(&"-".repeat(78));
        out.push('\n');
        for s in &self.steps {
            let (mark, extra) = match &s.verdict {
                Verdict::Pass => ("PASS", String::new()),
                Verdict::Fail {
                    reason,
                    observed,
                    expected,
                } => (
                    "FAIL",
                    format!(
                        "\n       原因: {reason}\n       观察: {observed}\n       期望: {expected}"
                    ),
                ),
            };
            out.push_str(&format!(
                "  [{mark}] #{seq:02} {name}\n         {detail}{extra}\n",
                seq = s.seq,
                name = s.name,
                detail = s.detail
            ));
        }
        let total = self.steps.len();
        let failed = self.steps.iter().filter(|s| !s.verdict.is_pass()).count();
        out.push_str(&"-".repeat(78));
        out.push('\n');
        out.push_str(&format!(
            "结论: {result}（{pass}/{total} 步通过，{failed} 步失败）\n",
            result = if failed == 0 { "全部通过" } else { "存在失败" },
            pass = total - failed,
        ));
        out
    }

    /// 机器可读 JSON（手写，避免引入额外依赖）。
    pub fn render_json(&self) -> String {
        let esc = |s: &str| {
            s.replace('\\', "\\\\")
                .replace('"', "\\\"")
                .replace('\n', "\\n")
        };
        let mut out = String::new();
        out.push_str("{\n");
        out.push_str(&format!("  \"id\": \"{}\",\n", esc(self.id)));
        out.push_str(&format!("  \"title\": \"{}\",\n", esc(self.title)));
        out.push_str(&format!("  \"run_id\": \"{}\",\n", esc(&self.run_id)));
        out.push_str(&format!("  \"core_version\": \"{}\",\n", self.core_version));
        out.push_str(&format!("  \"format_version\": {fv},\n", fv = self.format_version));
        out.push_str(&format!(
            "  \"started_unix_ms\": {},\n",
            self.started_unix_ms
        ));
        out.push_str(&format!(
            "  \"finished_unix_ms\": {},\n",
            self.finished_unix_ms
                .map(|v| v.to_string())
                .unwrap_or_else(|| "null".to_string())
        ));
        out.push_str(&format!("  \"passed\": {},\n", self.passed()));
        out.push_str("  \"steps\": [\n");
        for (i, s) in self.steps.iter().enumerate() {
            let (pass, reason, observed, expected) = match &s.verdict {
                Verdict::Pass => (true, "", "", ""),
                Verdict::Fail {
                    reason,
                    observed,
                    expected,
                } => (false, reason.as_str(), observed.as_str(), expected.as_str()),
            };
            out.push_str(&format!(
                "    {{\"seq\": {seq}, \"name\": \"{name}\", \"detail\": \"{detail}\", \"pass\": {pass}, \"reason\": \"{reason}\", \"observed\": \"{observed}\", \"expected\": \"{expected}\"}}{comma}\n",
                seq = s.seq,
                name = esc(&s.name),
                detail = esc(&s.detail),
                reason = esc(reason),
                observed = esc(observed),
                expected = esc(expected),
                comma = if i + 1 == self.steps.len() { "" } else { "," }
            ));
        }
        out.push_str("  ]\n}\n");
        out
    }
}

/// 场景内逐步记录器。
pub struct Reporter {
    report: ScenarioReport,
}

impl Reporter {
    pub fn new(id: &'static str, title: &'static str, run_id: String) -> Self {
        Self {
            report: ScenarioReport {
                id,
                title,
                run_id,
                core_version: cuckoo_core::CORE_VERSION,
                format_version: cuckoo_core::FORMAT_VERSION,
                started_unix_ms: now_ms(),
                finished_unix_ms: None,
                steps: Vec::new(),
            },
        }
    }

    /// 断言一个具体条件；失败时记录观察/期望与判定依据，不 panic、不吞掉。
    pub fn check(
        &mut self,
        name: impl Into<String>,
        detail: impl Into<String>,
        condition: bool,
        observed: impl Into<String>,
        expected: impl Into<String>,
    ) {
        let seq = self.report.steps.len() + 1;
        let verdict = if condition {
            Verdict::Pass
        } else {
            Verdict::Fail {
                reason: "断言条件不成立".to_string(),
                observed: observed.into(),
                expected: expected.into(),
            }
        };
        self.report.steps.push(StepRecord {
            seq,
            name: name.into(),
            detail: detail.into(),
            verdict,
        });
    }

    /// 等价性断言（观察值与期望值的 Debug 形式对比，支持切片等动态大小类型）。
    pub fn check_eq<T>(
        &mut self,
        name: impl Into<String>,
        detail: impl Into<String>,
        observed: &T,
        expected: &T,
    ) where
        T: PartialEq + fmt::Debug + ?Sized,
    {
        let cond = observed == expected;
        self.check(
            name,
            detail,
            cond,
            format!("{observed:#?}"),
            format!("{expected:#?}"),
        );
    }

    /// 记录一条始终通过的“计算步骤”（展示推导过程）。
    pub fn note(&mut self, name: impl Into<String>, detail: impl Into<String>) {
        let seq = self.report.steps.len() + 1;
        self.report.steps.push(StepRecord {
            seq,
            name: name.into(),
            detail: detail.into(),
            verdict: Verdict::Pass,
        });
    }

    pub fn fail(
        &mut self,
        name: impl Into<String>,
        detail: impl Into<String>,
        reason: impl Into<String>,
        observed: impl Into<String>,
        expected: impl Into<String>,
    ) {
        let seq = self.report.steps.len() + 1;
        self.report.steps.push(StepRecord {
            seq,
            name: name.into(),
            detail: detail.into(),
            verdict: Verdict::Fail {
                reason: reason.into(),
                observed: observed.into(),
                expected: expected.into(),
            },
        });
    }

    pub fn finish(mut self) -> ScenarioReport {
        self.report.finished_unix_ms = Some(now_ms());
        self.report
    }

    pub fn has_failures(&self) -> bool {
        self.report.steps.iter().any(|s| !s.verdict.is_pass())
    }
}

pub fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// 全局运行身份：时间戳 + PID，保证同一二进制多次执行可区分。
pub fn new_run_id() -> String {
    let pid = std::process::id();
    format!("verify-{}-{:x}", now_ms(), pid)
}
