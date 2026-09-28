//! DIMACS CNF 输入语言。
//!
//! 支持的方言：标准 DIMACS CNF（SAT 竞赛通用格式）。
//! - `c` 开头为注释行；`p cnf <变量数> <子句数>` 为问题头；
//! - 文字为非零整数，`0` 终止一个子句，子句可以跨行；
//! - 头部给出的子句数仅作一致性参考：不匹配时报为诊断而非硬错误；
//! - 文件末尾允许省略最后一个 `0`（容忍常见手写变体）。
//!
//! 解析与规范化严格分开：解析器只产出原始带符号整数子句，规范化由
//! [`crate::normalize`] 完成，使解析错误与规范化审计互不混淆。

use crate::normalize::{normalize_signed_clauses, NormalizedCnf};

/// 解析失败类别——每个变体都对应一条可断言的、具体的拒绝原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// 遇到头部之前出现了文字数据。
    DataBeforeHeader,
    /// 问题头不是 `p cnf <n> <m>` 的形状。
    MalformedHeader(String),
    /// 出现了无法识别的非法 token（非整数等）。
    IllegalToken(String),
    /// 头部声明 n 个变量，但文字引用了更大的变量号（越界）。
    VarOutOfBound { declared: usize, seen: i64 },
    /// 声明的子句数与实际终止的子句数不一致。
    ClauseCountMismatch { declared: i64, actual: usize },
    /// 完全没有问题头且内容非空。
    MissingHeader,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ParseError::DataBeforeHeader => {
                write!(f, "问题头 p cnf 之前出现了子句数据")
            }
            ParseError::MalformedHeader(h) => {
                write!(f, "问题头格式错误: {h:?}，应为 'p cnf <变量数> <子句数>'")
            }
            ParseError::IllegalToken(t) => write!(f, "无法识别的 token: {t:?}"),
            ParseError::VarOutOfBound { declared, seen } => write!(
                f,
                "文字 {seen} 引用的变量超过头部声明的变量数 {declared}"
            ),
            ParseError::ClauseCountMismatch { declared, actual } => write!(
                f,
                "头部声明 {declared} 个子句，实际解析到 {actual} 个"
            ),
            ParseError::MissingHeader => write!(f, "缺少 'p cnf' 问题头"),
        }
    }
}

impl std::error::Error for ParseError {}

/// 解析后的原始公式（未经规范化）。
#[derive(Debug, Clone)]
pub struct ParsedDimacs {
    pub num_vars: usize,
    pub num_clauses_declared: i64,
    pub clauses: Vec<Vec<i64>>,
}

/// 按空白切分但保留行结构，便于给出"第几行"式诊断。
pub fn parse_dimacs(input: &str) -> Result<ParsedDimacs, ParseError> {
    let mut header: Option<(usize, i64)> = None;
    let mut clauses: Vec<Vec<i64>> = Vec::new();
    let mut current: Vec<i64> = Vec::new();
    // 是否在头部之前见过任何数据 token。
    let mut saw_data_before_header = false;

    for (line_no, raw_line) in input.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() {
            continue;
        }
        // 行注释：c 开头。DIMACS 还允许 '%' 作为扩展段分隔，直接忽略整行。
        if line.starts_with('c') || line.starts_with('%') {
            continue;
        }
        if line.starts_with('p') {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() < 4 || parts[0] != "p" || parts[1] != "cnf" {
                return Err(ParseError::MalformedHeader(line.to_string()));
            }
            let n: i64 = parts[2].parse().map_err(|_| {
                ParseError::MalformedHeader(line.to_string())
            })?;
            let m: i64 = parts[3].parse().map_err(|_| {
                ParseError::MalformedHeader(line.to_string())
            })?;
            if n < 0 || m < 0 {
                return Err(ParseError::MalformedHeader(line.to_string()));
            }
            header = Some((n as usize, m));
            continue;
        }

        for tok in line.split_whitespace() {
            let value: i64 = match tok.parse() {
                Ok(v) => v,
                Err(_) => {
                    return Err(ParseError::IllegalToken(format!(
                        "第 {} 行: {tok}",
                        line_no + 1
                    )))
                }
            };
            if header.is_none() && value != 0 {
                saw_data_before_header = true;
            }
            if value == 0 {
                clauses.push(std::mem::take(&mut current));
            } else {
                current.push(value);
            }
        }
    }

    if saw_data_before_header {
        return Err(ParseError::DataBeforeHeader);
    }

    // 末尾未终止的残子句：容忍，自成一条。
    if !current.is_empty() {
        clauses.push(std::mem::take(&mut current));
    }

    let (num_vars, num_clauses_declared) = match header {
        Some(h) => h,
        None => {
            if clauses.is_empty() {
                // 空文件视为空公式（SAT，模型为空），这是明确的边界行为。
                (0, 0)
            } else {
                return Err(ParseError::MissingHeader);
            }
        }
    };

    if clauses.len() as i64 != num_clauses_declared {
        return Err(ParseError::ClauseCountMismatch {
            declared: num_clauses_declared,
            actual: clauses.len(),
        });
    }

    for clause in &clauses {
        for &l in clause {
            let v = l.unsigned_abs() as i64;
            if v as usize > num_vars {
                return Err(ParseError::VarOutOfBound {
                    declared: num_vars,
                    seen: l,
                });
            }
        }
    }

    Ok(ParsedDimacs {
        num_vars,
        num_clauses_declared,
        clauses,
    })
}

/// 一步到位：解析 DIMACS 并规范化。
pub fn parse_and_normalize(input: &str) -> Result<NormalizedCnf, ParseError> {
    let parsed = parse_dimacs(input)?;
    Ok(normalize_signed_clauses(parsed.num_vars, &parsed.clauses))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_basic_cnf_with_comments_and_cross_line_clause() {
        let src = "c comment\np cnf 3 2\n1 -2 0\n3 1\n0\n";
        let p = parse_dimacs(src).unwrap();
        assert_eq!(p.num_vars, 3);
        assert_eq!(p.clauses, vec![vec![1, -2], vec![3, 1]]);
    }

    #[test]
    fn empty_file_is_empty_formula() {
        let n = parse_and_normalize("").unwrap();
        assert_eq!(n.effective_vars(), 0);
        assert!(n.clauses.is_empty());
        assert!(!n.has_empty_clause());
    }

    #[test]
    fn rejects_data_before_header() {
        let err = parse_dimacs("1 0\np cnf 1 1\n").unwrap_err();
        assert_eq!(err, ParseError::DataBeforeHeader);
    }

    #[test]
    fn rejects_out_of_bound_variable() {
        let err = parse_dimacs("p cnf 2 1\n1 3 0\n").unwrap_err();
        assert!(matches!(
            err,
            ParseError::VarOutOfBound {
                declared: 2,
                seen: 3
            }
        ));
    }

    #[test]
    fn rejects_clause_count_mismatch() {
        let err = parse_dimacs("p cnf 2 3\n1 0\n-2 0\n").unwrap_err();
        assert_eq!(
            err,
            ParseError::ClauseCountMismatch {
                declared: 3,
                actual: 2
            }
        );
    }

    #[test]
    fn explicit_empty_clause_survives_as_empty() {
        let n = parse_and_normalize("p cnf 1 1\n0\n").unwrap();
        assert!(n.has_empty_clause());
    }
}
