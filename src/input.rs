//! 输入语言：DIMACS CNF 解析（与结构化 JSON 入口）。
//!
//! 支持的 DIMACS 子集：
//! - 行/块注释：`c` 开头；
//! - 问题行：`p cnf <变量数> <子句数>`（子句数允许省略，给了则校验）；
//! - 子句：以 `0` 结尾；允许跨物理行，也允许一条逻辑行上放多条子句；
//! - 不以 `0` 结尾但遇到 EOF 时，已累积的文字自动闭合为最后一条子句。
//!
//! 解析器只做语法处理；规范化（去重/重言式/空子句）属于 [`crate::cnf`] 的职责。

use serde::Deserialize;

use crate::cnf::{Lit, NormError};

/// 解析得到的原始公式：声明变量数（若问题行给出）与原始子句序列。
#[derive(Debug, Clone)]
pub struct ParsedCnf {
    pub declared_vars: Option<usize>,
    pub clauses: Vec<Vec<Lit>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    BadToken(String),
    BadProblemLine(String),
    DeclaredClauseCountMismatch { declared: usize, actual: usize },
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ParseError::BadToken(t) => write!(f, "cannot parse token: {t:?}"),
            ParseError::BadProblemLine(l) => {
                write!(
                    f,
                    "malformed problem line, expected 'p cnf <vars> <clauses>': {l:?}"
                )
            }
            ParseError::DeclaredClauseCountMismatch { declared, actual } => write!(
                f,
                "problem line declares {declared} clauses but input contains {actual}"
            ),
        }
    }
}

impl std::error::Error for ParseError {}

/// 解析 DIMACS CNF 文本。
pub fn parse_dimacs(input: &str) -> Result<ParsedCnf, ParseError> {
    let mut declared_vars: Option<usize> = None;
    let mut declared_clauses: Option<usize> = None;
    let mut clauses: Vec<Vec<Lit>> = Vec::new();
    let mut current: Vec<Lit> = Vec::new();

    for raw_line in input.lines() {
        let line = raw_line.trim();
        if line.is_empty() {
            continue;
        }
        // 注释：c 或 C 起头；% 是传统的 EOF 标记，之后内容忽略。
        if line.starts_with(['c', 'C']) {
            continue;
        }
        if line.starts_with('%') {
            break;
        }
        if line.starts_with('p') {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() < 4 || parts[0] != "p" || parts[1] != "cnf" {
                return Err(ParseError::BadProblemLine(line.to_string()));
            }
            let nv: usize = parts[2]
                .parse()
                .map_err(|_| ParseError::BadProblemLine(line.to_string()))?;
            let nc: usize = parts[3]
                .parse()
                .map_err(|_| ParseError::BadProblemLine(line.to_string()))?;
            declared_vars = Some(nv);
            declared_clauses = Some(nc);
            continue;
        }
        for tok in line.split_whitespace() {
            let n: i64 = tok
                .parse()
                .map_err(|_| ParseError::BadToken(tok.to_string()))?;
            if n == 0 {
                // 结束当前子句；current 为空即空子句（连续 0 产生多个空子句，属于冗余输入）。
                clauses.push(std::mem::take(&mut current));
            } else if !(i32::MIN as i64..=i32::MAX as i64).contains(&n) {
                return Err(ParseError::BadToken(tok.to_string()));
            } else {
                current.push(n as Lit);
            }
        }
    }
    // EOF 未写终止 0：自动闭合最后一条非空挂起子句。
    if !current.is_empty() {
        clauses.push(current);
    }

    if let Some(declared) = declared_clauses {
        if declared != clauses.len() {
            return Err(ParseError::DeclaredClauseCountMismatch {
                declared,
                actual: clauses.len(),
            });
        }
    }
    Ok(ParsedCnf {
        declared_vars,
        clauses,
    })
}

/// 结构化请求体（HTTP API 使用）。
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JsonCnfRequest {
    /// 变量数；省略时从文字推断。
    #[serde(default)]
    pub num_vars: Option<usize>,
    /// 每条子句是有符号整数数组，例如 `[1, -2]` 表示 x1 ∨ ¬x2；空数组表示空子句。
    #[serde(default)]
    pub clauses: Vec<Vec<Lit>>,
    /// 可选：以 DIMACS 文本提供，提供时忽略 `clauses`。
    #[serde(default)]
    pub dimacs: Option<String>,
}

impl JsonCnfRequest {
    /// 转为 [`ParsedCnf`]。DIMACS 优先（显式给出时）。
    pub fn into_parsed(self) -> Result<ParsedCnf, ParseError> {
        if let Some(text) = self.dimacs {
            return parse_dimacs(&text);
        }
        Ok(ParsedCnf {
            declared_vars: self.num_vars,
            clauses: self.clauses,
        })
    }
}

/// 把规范化阶段的错误也归入统一的“输入错误”类型，供 API 层映射 HTTP 400。
#[derive(Debug)]
pub enum InputError {
    Parse(ParseError),
    Normalize(NormError),
}

impl std::fmt::Display for InputError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            InputError::Parse(e) => write!(f, "{e}"),
            InputError::Normalize(e) => write!(f, "{e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_basic_dimacs() {
        let p = parse_dimacs("c demo\np cnf 3 2\n1 -2 0\n2 3 0\n").unwrap();
        assert_eq!(p.declared_vars, Some(3));
        assert_eq!(p.clauses, vec![vec![1, -2], vec![2, 3]]);
    }

    #[test]
    fn clauses_may_span_and_share_lines() {
        let p = parse_dimacs("1 2\n0 3 0").unwrap();
        assert_eq!(p.clauses, vec![vec![1, 2], vec![3]]);
    }

    #[test]
    fn unterminated_last_clause_is_closed_on_eof() {
        let p = parse_dimacs("1 -2 3").unwrap();
        assert_eq!(p.clauses, vec![vec![1, -2, 3]]);
    }

    #[test]
    fn explicit_empty_clause() {
        let p = parse_dimacs("p cnf 1 1\n0").unwrap();
        assert_eq!(p.clauses, vec![Vec::<Lit>::new()]);
    }

    #[test]
    fn detects_count_mismatch() {
        assert_eq!(
            parse_dimacs("p cnf 2 2\n1 0").unwrap_err(),
            ParseError::DeclaredClauseCountMismatch {
                declared: 2,
                actual: 1
            }
        );
    }

    #[test]
    fn rejects_bad_token() {
        assert!(matches!(
            parse_dimacs("1 x 0").unwrap_err(),
            ParseError::BadToken(_)
        ));
    }
}
