//! 输入语言模块。
//!
//! 职责（与求解内核严格分离）：
//! - 布尔表达式的语法树 [`Expr`]（`serde` 外部标签 JSON，见 `samples/`）；
//! - 文本语法解析器（[`parse`]），语法见 `docs/lang.md`；
//! - 与 BDD 实现无关的直接递归解释器 [`Expr::eval`]，证据侧用它充当
//!   独立真值预言机——它不经过唯一表、apply 或补集边的任何代码路径。

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// 布尔表达式语法树。
///
/// JSON 表示采用 serde 外部标签，例如 `{"Not":{"Var":"a"}}`、
/// `{"And":[{"Var":"a"},{"Const":true}]}`。`Const` 与 `Var` 单元形负载
/// 反序列化时可写作 `"Const"` / `"Const":true` 等多种等价形式。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Expr {
    /// 布尔常量。
    Const(bool),
    /// 变量引用。
    Var(String),
    /// 逻辑非。
    Not(Box<Expr>),
    /// 逻辑与（n 元，空表视为常量 `true`）。
    And(Vec<Expr>),
    /// 逻辑或（n 元，空表视为常量 `false`）。
    Or(Vec<Expr>),
    /// 异或（n 元，按左结合链求值）。
    Xor(Vec<Expr>),
    /// 蕴含 `a -> b`（二元）。
    Implies(Box<Expr>, Box<Expr>),
    /// 同或/双向蕴含 `a <-> b`（二元）。
    Iff(Box<Expr>, Box<Expr>),
}

impl Expr {
    /// 变量出现集合，按首次出现顺序收集。
    pub fn vars(&self) -> Vec<String> {
        let mut out = Vec::new();
        self.collect_vars(&mut out);
        out
    }

    fn collect_vars(&self, out: &mut Vec<String>) {
        match self {
            Expr::Const(_) => {}
            Expr::Var(name) => {
                if !out.contains(name) {
                    out.push(name.clone());
                }
            }
            Expr::Not(e) => e.collect_vars(out),
            Expr::And(es) | Expr::Or(es) | Expr::Xor(es) => {
                for e in es {
                    e.collect_vars(out);
                }
            }
            Expr::Implies(a, b) | Expr::Iff(a, b) => {
                a.collect_vars(out);
                b.collect_vars(out);
            }
        }
    }

    /// 直接递归求值。未在 `env` 中给出的变量按 `false` 处理
    /// （验证侧总是传入完整赋值，因此该兜底仅用于防御）。
    ///
    /// 该实现刻意不依赖 `core` 模块，作为独立参考。
    pub fn eval(&self, env: &HashMap<String, bool>) -> bool {
        match self {
            Expr::Const(v) => *v,
            Expr::Var(name) => env.get(name).copied().unwrap_or(false),
            Expr::Not(e) => !e.eval(env),
            Expr::And(es) => es.iter().all(|e| e.eval(env)),
            Expr::Or(es) => es.iter().any(|e| e.eval(env)),
            Expr::Xor(es) => es.iter().fold(false, |acc, e| acc ^ e.eval(env)),
            Expr::Implies(a, b) => !a.eval(env) || b.eval(env),
            Expr::Iff(a, b) => a.eval(env) == b.eval(env),
        }
    }

    /// 按映射重命名变量；`renaming` 中未出现的变量保持原名。
    pub fn rename(&self, renaming: &HashMap<String, String>) -> Expr {
        match self {
            Expr::Const(v) => Expr::Const(*v),
            Expr::Var(name) => {
                Expr::Var(renaming.get(name).cloned().unwrap_or_else(|| name.clone()))
            }
            Expr::Not(e) => Expr::Not(Box::new(e.rename(renaming))),
            Expr::And(es) => Expr::And(es.iter().map(|e| e.rename(renaming)).collect()),
            Expr::Or(es) => Expr::Or(es.iter().map(|e| e.rename(renaming)).collect()),
            Expr::Xor(es) => Expr::Xor(es.iter().map(|e| e.rename(renaming)).collect()),
            Expr::Implies(a, b) => {
                Expr::Implies(Box::new(a.rename(renaming)), Box::new(b.rename(renaming)))
            }
            Expr::Iff(a, b) => {
                Expr::Iff(Box::new(a.rename(renaming)), Box::new(b.rename(renaming)))
            }
        }
    }
}

pub mod parser;
