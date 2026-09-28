//! 文本语法解析器。
//!
//! 语法（`docs/lang.md` 有完整描述）：
//!
//! ```text
//! expr    := iff
//! iff     := implies ("<->" implies)*
//! implies := xor ("->" xor)*            // 左结合
//! xor     := or ("^" or)*               // 比 implies 结合更紧
//! or      := and ("|" and)*             // 也接受 "or"
//! and     := unary ("&" unary)*         // 也接受 "and"
//! unary   := "!" unary | atom
//! atom    := "true" | "false" | ident | "(" expr ")"
//! ```
//!
//! 标识符规则：以字母或下划线开头，后跟字母、数字、下划线。
//! `and`/`or` 是关键字，不能作为变量名；`true`/`false`/`not` 同理。

use super::Expr;

/// 解析错误。字节偏移基于输入字符串的字符位置（0 起）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError {
    /// 出错位置（字符偏移）。
    pub pos: usize,
    /// 面向用户的英文原因串。
    pub message: String,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "parse error at {}: {}", self.pos, self.message)
    }
}

impl std::error::Error for ParseError {}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Tok {
    Ident(String),
    True,
    False,
    And,
    Or,
    Not,
    Bang,
    Amp,
    Pipe,
    Caret,
    Arrow,
    Iff,
    LParen,
    RParen,
}

#[derive(Debug, Clone)]
struct Spanned {
    tok: Tok,
    pos: usize,
}

fn tokenize(src: &str) -> Result<Vec<Spanned>, ParseError> {
    let chars: Vec<char> = src.chars().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        match c {
            ' ' | '\t' | '\n' | '\r' => i += 1,
            '!' => {
                out.push(Spanned {
                    tok: Tok::Bang,
                    pos: i,
                });
                i += 1;
            }
            '&' => {
                out.push(Spanned {
                    tok: Tok::Amp,
                    pos: i,
                });
                i += 1;
            }
            '|' => {
                out.push(Spanned {
                    tok: Tok::Pipe,
                    pos: i,
                });
                i += 1;
            }
            '^' => {
                out.push(Spanned {
                    tok: Tok::Caret,
                    pos: i,
                });
                i += 1;
            }
            '(' => {
                out.push(Spanned {
                    tok: Tok::LParen,
                    pos: i,
                });
                i += 1;
            }
            ')' => {
                out.push(Spanned {
                    tok: Tok::RParen,
                    pos: i,
                });
                i += 1;
            }
            '-' if i + 1 < chars.len() && chars[i + 1] == '>' => {
                out.push(Spanned {
                    tok: Tok::Arrow,
                    pos: i,
                });
                i += 2;
            }
            '<' if i + 2 < chars.len() && chars[i + 1] == '-' && chars[i + 2] == '>' => {
                out.push(Spanned {
                    tok: Tok::Iff,
                    pos: i,
                });
                i += 3;
            }
            c if c.is_ascii_alphabetic() || c == '_' => {
                let start = i;
                while i < chars.len() && (chars[i].is_ascii_alphanumeric() || chars[i] == '_') {
                    i += 1;
                }
                let word: String = chars[start..i].iter().collect();
                let tok = match word.as_str() {
                    "true" => Tok::True,
                    "false" => Tok::False,
                    "and" => Tok::And,
                    "or" => Tok::Or,
                    "not" => Tok::Not,
                    _ => Tok::Ident(word),
                };
                out.push(Spanned { tok, pos: start });
            }
            other => {
                return Err(ParseError {
                    pos: i,
                    message: format!("unexpected character {other:?}"),
                });
            }
        }
    }
    Ok(out)
}

struct Parser {
    toks: Vec<Spanned>,
    cur: usize,
}

impl Parser {
    fn peek(&self) -> Option<&Spanned> {
        self.toks.get(self.cur)
    }

    fn bump(&mut self) -> Option<Spanned> {
        let t = self.toks.get(self.cur).cloned();
        if t.is_some() {
            self.cur += 1;
        }
        t
    }

    fn err<T>(&self, pos: usize, message: impl Into<String>) -> Result<T, ParseError> {
        Err(ParseError {
            pos,
            message: message.into(),
        })
    }

    fn parse_expr(&mut self) -> Result<Expr, ParseError> {
        self.parse_iff()
    }

    fn parse_left_assoc(
        &mut self,
        sub: fn(&mut Self) -> Result<Expr, ParseError>,
        is_op: fn(&Tok) -> bool,
        make: fn(Expr, Expr) -> Expr,
    ) -> Result<Expr, ParseError> {
        let mut lhs = sub(self)?;
        while let Some(sp) = self.peek() {
            if is_op(&sp.tok) {
                self.bump();
                let rhs = sub(self)?;
                lhs = make(lhs, rhs);
            } else {
                break;
            }
        }
        Ok(lhs)
    }

    fn parse_iff(&mut self) -> Result<Expr, ParseError> {
        self.parse_left_assoc(
            Self::parse_implies,
            |t| matches!(t, Tok::Iff),
            |a, b| Expr::Iff(Box::new(a), Box::new(b)),
        )
    }

    fn parse_implies(&mut self) -> Result<Expr, ParseError> {
        self.parse_left_assoc(
            Self::parse_xor,
            |t| matches!(t, Tok::Arrow),
            |a, b| Expr::Implies(Box::new(a), Box::new(b)),
        )
    }

    fn parse_xor(&mut self) -> Result<Expr, ParseError> {
        // Xor 在 AST 中是 n 元链。
        let mut items = vec![self.parse_or()?];
        while let Some(sp) = self.peek() {
            if matches!(sp.tok, Tok::Caret) {
                self.bump();
                items.push(self.parse_or()?);
            } else {
                break;
            }
        }
        if items.len() == 1 {
            Ok(items.pop().unwrap())
        } else {
            Ok(Expr::Xor(items))
        }
    }

    fn parse_or(&mut self) -> Result<Expr, ParseError> {
        let mut items = vec![self.parse_and()?];
        while let Some(sp) = self.peek() {
            if matches!(sp.tok, Tok::Pipe | Tok::Or) {
                self.bump();
                items.push(self.parse_and()?);
            } else {
                break;
            }
        }
        if items.len() == 1 {
            Ok(items.pop().unwrap())
        } else {
            Ok(Expr::Or(items))
        }
    }

    fn parse_and(&mut self) -> Result<Expr, ParseError> {
        let mut items = vec![self.parse_unary()?];
        while let Some(sp) = self.peek() {
            if matches!(sp.tok, Tok::Amp | Tok::And) {
                self.bump();
                items.push(self.parse_unary()?);
            } else {
                break;
            }
        }
        if items.len() == 1 {
            Ok(items.pop().unwrap())
        } else {
            Ok(Expr::And(items))
        }
    }

    fn parse_unary(&mut self) -> Result<Expr, ParseError> {
        if let Some(sp) = self.peek().cloned() {
            match sp.tok {
                Tok::Bang | Tok::Not => {
                    self.bump();
                    Ok(Expr::Not(Box::new(self.parse_unary()?)))
                }
                _ => self.parse_atom(),
            }
        } else {
            self.err(self.toks.len(), "expected expression, found end of input")
        }
    }

    fn parse_atom(&mut self) -> Result<Expr, ParseError> {
        let sp = self.peek().cloned().ok_or_else(|| ParseError {
            pos: 0,
            message: "expected expression, found empty input".to_string(),
        })?;
        match sp.tok {
            Tok::True => {
                self.bump();
                Ok(Expr::Const(true))
            }
            Tok::False => {
                self.bump();
                Ok(Expr::Const(false))
            }
            Tok::Ident(name) => {
                self.bump();
                Ok(Expr::Var(name))
            }
            Tok::LParen => {
                self.bump();
                let e = self.parse_expr()?;
                match self.bump() {
                    Some(closing) if matches!(closing.tok, Tok::RParen) => Ok(e),
                    Some(other) => self.err(other.pos, "expected ')'"),
                    None => self.err(sp.pos, "unclosed '('"),
                }
            }
            _ => self.err(sp.pos, "expected variable, constant or '('"),
        }
    }
}

/// 解析文本表达式。
pub fn parse(src: &str) -> Result<Expr, ParseError> {
    let toks = tokenize(src)?;
    if toks.is_empty() {
        return Err(ParseError {
            pos: 0,
            message: "empty expression".to_string(),
        });
    }
    let mut p = Parser { toks, cur: 0 };
    let expr = p.parse_expr()?;
    if let Some(leftover) = p.peek() {
        return Err(ParseError {
            pos: leftover.pos,
            message: "unexpected token after complete expression".to_string(),
        });
    }
    Ok(expr)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    #[test]
    fn parses_precedence_and_constants() {
        let e = parse("a & b | c").unwrap();
        assert_eq!(
            e,
            Expr::Or(vec![
                Expr::And(vec![Expr::Var("a".into()), Expr::Var("b".into())]),
                Expr::Var("c".into()),
            ])
        );
        assert_eq!(parse("true").unwrap(), Expr::Const(true));
        assert_eq!(
            parse("!false").unwrap(),
            Expr::Not(Box::new(Expr::Const(false)))
        );
    }

    #[test]
    fn parses_implies_left_associative_with_xor_above_it() {
        let e = parse("a -> b -> c").unwrap();
        assert_eq!(
            e,
            Expr::Implies(
                Box::new(Expr::Implies(
                    Box::new(Expr::Var("a".into())),
                    Box::new(Expr::Var("b".into()))
                )),
                Box::new(Expr::Var("c".into())),
            )
        );
        // xor 结合比 implies 紧：a -> b ^ c = a -> (b ^ c)
        let e = parse("a -> b ^ c").unwrap();
        assert_eq!(
            e,
            Expr::Implies(
                Box::new(Expr::Var("a".into())),
                Box::new(Expr::Xor(vec![
                    Expr::Var("b".into()),
                    Expr::Var("c".into())
                ]))
            )
        );
        assert!(parse("a <-> b ^ c").is_ok());
    }

    #[test]
    fn rejects_bad_inputs_with_positions() {
        let e1 = parse("a &").unwrap_err();
        assert!(e1.message.contains("expected"));
        let e2 = parse("(a").unwrap_err();
        assert!(e2.message.contains("'('"));
        let e3 = parse("a b").unwrap_err();
        assert_eq!(e3.pos, 2);
        assert!(parse("").is_err());
        assert!(parse("a $ b").is_err());
    }

    #[test]
    fn parser_eval_matches_hand_checked_truth_table() {
        // a -> (b xor c) 的独立小真值表（手算；a=0 恒真，a=1 时为 b⊕c）。
        let e = parse("a -> b ^ c").unwrap();
        let cases = [
            // (a, b, c, expected)
            (false, false, false, true),
            (false, true, false, true),
            (false, false, true, true),
            (false, true, true, true),
            (true, false, false, false),
            (true, true, false, true),
            (true, false, true, true),
            (true, true, true, false),
        ];
        for (a, b, c, want) in cases {
            let env = HashMap::from([
                ("a".to_string(), a),
                ("b".to_string(), b),
                ("c".to_string(), c),
            ]);
            assert_eq!(e.eval(&env), want, "a={a} b={b} c={c}");
        }
    }
}
