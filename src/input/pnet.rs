//! `.pnet` 文本语言。
//!
//! 语法（区分大小写关键字；`#` 起到行尾为注释；语句以 `;` 结束）：
//!
//! ```text
//! place waiting : capacity 1;
//! place buffer  : capacity 3;
//!
//! transition produce:
//!     consumes;
//!     produces 2 buffer;
//!
//! transition consume:
//!     consumes 2 buffer;
//!     produces;
//! ```
//!
//! - `consumes` / `produces` 后接 `数量 库所名` 对，逗号分隔；均可为空。
//! - 标识符规则：字母/下划线开头，后接字母、数字、下划线。
//! - 所有数字必须是非负 64 位整数，数值安全上界由统一语义校验负责。

use super::{codes, build_net, InputError, NetSpec, PlaceSpec, TransitionSpec};
use crate::kernel::model::Net;

#[derive(Debug, Clone, PartialEq, Eq)]
enum Tok {
    Place,
    Transition,
    Capacity,
    Colon,
    Semicolon,
    Comma,
    Consumes,
    Produces,
    Ident(String),
    Number(i64),
}

#[derive(Debug, Clone)]
struct Lexed {
    tok: Tok,
    line: usize,
    col: usize,
}

fn lex(src: &str) -> Result<Vec<Lexed>, InputError> {
    let bytes: Vec<char> = src.chars().collect();
    let mut out = Vec::new();
    let mut i = 0usize;
    let mut line = 1usize;
    let mut col = 1usize;

    let err = |code, msg: String, line, col| InputError::one(code, msg, Some(format!("{line}:{col}")));

    while i < bytes.len() {
        let c = bytes[i];
        match c {
            '#' => {
                while i < bytes.len() && bytes[i] != '\n' {
                    i += 1;
                    col += 1;
                }
            }
            ' ' | '\t' | '\r' => {
                i += 1;
                col += 1;
            }
            '\n' => {
                i += 1;
                line += 1;
                col = 1;
            }
            ':' => {
                out.push(Lexed {
                    tok: Tok::Colon,
                    line,
                    col,
                });
                i += 1;
                col += 1;
            }
            ';' => {
                out.push(Lexed {
                    tok: Tok::Semicolon,
                    line,
                    col,
                });
                i += 1;
                col += 1;
            }
            ',' => {
                out.push(Lexed {
                    tok: Tok::Comma,
                    line,
                    col,
                });
                i += 1;
                col += 1;
            }
            '0'..='9' => {
                let start = i;
                let start_col = col;
                while i < bytes.len() && bytes[i].is_ascii_digit() {
                    i += 1;
                    col += 1;
                }
                let text: String = bytes[start..i].iter().collect();
                let value = text.parse::<i64>().map_err(|_| {
                    err(
                        codes::VALUE_OUT_OF_RANGE,
                        format!("number {text} does not fit in signed 64-bit integer"),
                        line,
                        start_col,
                    )
                })?;
                out.push(Lexed {
                    tok: Tok::Number(value),
                    line,
                    col: start_col,
                });
            }
            'a'..='z' | 'A'..='Z' | '_' => {
                let start = i;
                let start_col = col;
                while i < bytes.len()
                    && (bytes[i].is_ascii_alphanumeric() || bytes[i] == '_')
                {
                    i += 1;
                    col += 1;
                }
                let word: String = bytes[start..i].iter().collect();
                let tok = match word.as_str() {
                    "place" => Tok::Place,
                    "transition" => Tok::Transition,
                    "capacity" => Tok::Capacity,
                    "consumes" => Tok::Consumes,
                    "produces" => Tok::Produces,
                    _ => Tok::Ident(word),
                };
                out.push(Lexed {
                    tok,
                    line,
                    col: start_col,
                });
            }
            other => {
                return Err(err(
                    codes::PARSE_ERROR,
                    format!("unexpected character '{other}'"),
                    line,
                    col,
                ));
            }
        }
    }
    Ok(out)
}

struct Parser<'a> {
    toks: &'a [Lexed],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn peek(&self) -> Option<&Lexed> {
        self.toks.get(self.pos)
    }

    fn at(&self, t: &Tok) -> bool {
        matches!(self.peek(), Some(l) if std::mem::discriminant(&l.tok) == std::mem::discriminant(t))
    }

    fn bump(&mut self) -> Option<&'a Lexed> {
        let l = self.toks.get(self.pos);
        if l.is_some() {
            self.pos += 1;
        }
        l
    }

    fn expect(&mut self, t: Tok, what: &str) -> Result<(), InputError> {
        if self.at(&t) {
            self.bump();
            Ok(())
        } else {
            let (line, col, found) = match self.peek() {
                Some(l) => (l.line, l.col, tok_name(&l.tok)),
                None => (0usize, 0usize, "end of file".to_string()),
            };
            Err(InputError::one(
                codes::PARSE_ERROR,
                format!("expected {what}, found {found}"),
                if line == 0 {
                    None
                } else {
                    Some(format!("{line}:{col}"))
                },
            ))
        }
    }

    fn expect_ident(&mut self, what: &str) -> Result<(String, usize, usize), InputError> {
        match self.peek() {
            Some(Lexed {
                tok: Tok::Ident(s),
                line,
                col,
            }) => {
                let (s, line, col) = (s.clone(), *line, *col);
                self.pos += 1;
                Ok((s, line, col))
            }
            Some(l) => Err(InputError::one(
                codes::PARSE_ERROR,
                format!("expected {what} (identifier), found {}", tok_name(&l.tok)),
                Some(format!("{}:{}", l.line, l.col)),
            )),
            None => Err(InputError::one(
                codes::PARSE_ERROR,
                format!("expected {what} (identifier), found end of file"),
                None,
            )),
        }
    }
}

fn tok_name(t: &Tok) -> String {
    match t {
        Tok::Place => "'place'".into(),
        Tok::Transition => "'transition'".into(),
        Tok::Capacity => "'capacity'".into(),
        Tok::Colon => "':'".into(),
        Tok::Semicolon => "';'".into(),
        Tok::Comma => "','".into(),
        Tok::Consumes => "'consumes'".into(),
        Tok::Produces => "'produces'".into(),
        Tok::Ident(s) => format!("identifier '{s}'"),
        Tok::Number(n) => format!("number {n}"),
    }
}

/// 解析 `.pnet` 文本（文本格式不携带初标识；初标识走 JSON 请求体）。
pub fn parse_pnet(src: &str) -> Result<Net, InputError> {
    let toks = lex(src)?;
    let mut p = Parser {
        toks: &toks,
        pos: 0,
    };

    let mut spec = NetSpec {
        places: Vec::new(),
        transitions: Vec::new(),
        initial_marking: None,
    };

    while p.peek().is_some() {
        match p.peek().map(|l| &l.tok) {
            Some(Tok::Place) => {
                p.bump();
                let (name, _, _) = p.expect_ident("place name")?;
                p.expect(Tok::Colon, "':'")?;
                p.expect(Tok::Capacity, "'capacity'")?;
                let cap_tok = match p.bump() {
                    Some(Lexed {
                        tok: Tok::Number(n),
                        ..
                    }) => *n,
                    Some(l) => {
                        return Err(InputError::one(
                            codes::BAD_NUMBER,
                            format!("expected capacity number, found {}", tok_name(&l.tok)),
                            Some(format!("{}:{}", l.line, l.col)),
                        ))
                    }
                    None => {
                        return Err(InputError::one(
                            codes::BAD_NUMBER,
                            "expected capacity number, found end of file",
                            None,
                        ))
                    }
                };
                p.expect(Tok::Semicolon, "';'")?;
                spec.places.push(PlaceSpec {
                    name,
                    capacity: cap_tok,
                });
            }
            Some(Tok::Transition) => {
                p.bump();
                let (name, _, _) = p.expect_ident("transition name")?;
                p.expect(Tok::Colon, "':'")?;
                let mut ts = TransitionSpec {
                    name,
                    ..Default::default()
                };
                // consumes / produces 两个子句，顺序不限但各至多出现一次。
                let mut saw_consumes = false;
                let mut saw_produces = false;
                loop {
                    if p.at(&Tok::Consumes) {
                        if saw_consumes {
                            let l = p.peek().unwrap();
                            return Err(InputError::one(
                                codes::PARSE_ERROR,
                                "duplicate 'consumes' clause in transition",
                                Some(format!("{}:{}", l.line, l.col)),
                            ));
                        }
                        saw_consumes = true;
                        p.bump();
                        parse_arc_list(&mut p, &mut ts.inputs)?;
                        p.expect(Tok::Semicolon, "';' after consumes clause")?;
                    } else if p.at(&Tok::Produces) {
                        if saw_produces {
                            let l = p.peek().unwrap();
                            return Err(InputError::one(
                                codes::PARSE_ERROR,
                                "duplicate 'produces' clause in transition",
                                Some(format!("{}:{}", l.line, l.col)),
                            ));
                        }
                        saw_produces = true;
                        p.bump();
                        parse_arc_list(&mut p, &mut ts.outputs)?;
                        p.expect(Tok::Semicolon, "';' after produces clause")?;
                    } else {
                        break;
                    }
                }
                spec.transitions.push(ts);
            }
            Some(other) => {
                let l = p.peek().unwrap();
                return Err(InputError::one(
                    codes::PARSE_ERROR,
                    format!(
                        "expected 'place' or 'transition' statement, found {}",
                        tok_name(other)
                    ),
                    Some(format!("{}:{}", l.line, l.col)),
                ));
            }
            None => break,
        }
    }

    build_net(&spec)
}

/// 解析 `数量 库所 (, 数量 库所)*`（可空）。
fn parse_arc_list(p: &mut Parser, arcs: &mut Vec<(String, i64)>) -> Result<(), InputError> {
    // 空列表直接以 ';' 结束（由调用方消费）。
    if p.at(&Tok::Semicolon) {
        return Ok(());
    }
    loop {
        let weight = match p.bump() {
            Some(Lexed {
                tok: Tok::Number(n),
                ..
            }) => *n,
            Some(l) => {
                return Err(InputError::one(
                    codes::BAD_NUMBER,
                    format!("expected arc weight number, found {}", tok_name(&l.tok)),
                    Some(format!("{}:{}", l.line, l.col)),
                ))
            }
            None => {
                return Err(InputError::one(
                    codes::BAD_NUMBER,
                    "expected arc weight number, found end of file",
                    None,
                ))
            }
        };
        let (pname, line, col) = p.expect_ident("place name after arc weight")?;
        arcs.push((pname, weight));
        let _ = (line, col);
        if p.at(&Tok::Comma) {
            p.bump();
            continue;
        }
        break;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::input::codes;

    const MUTEX_SRC: &str = r#"
        # 互斥资源网
        place free   : capacity 1;
        place idle_a : capacity 1;
        place crit_a : capacity 1;
        transition enter_a:
            consumes 1 idle_a, 1 free;
            produces 1 crit_a;
        transition leave_a:
            consumes 1 crit_a;
            produces 1 idle_a, 1 free;
    "#;

    #[test]
    fn parses_pnet_with_comments_and_empty_arcs() {
        let net = parse_pnet(MUTEX_SRC).expect("parse ok");
        assert_eq!(net.place_count(), 3);
        assert_eq!(net.transition_count(), 2);
        let enter = &net.transitions[0];
        assert_eq!(enter.inputs.len(), 2);
        assert_eq!(enter.outputs.len(), 1);
        // 弧按库所索引排序。
        assert!(enter.inputs[0].place <= enter.inputs[1].place);
    }

    #[test]
    fn empty_consumes_clause_is_legal() {
        let src = "place p : capacity 2;\n\
                   transition gen: consumes; produces 1 p;";
        let net = parse_pnet(src).unwrap();
        assert!(net.transitions[0].inputs.is_empty());
        assert_eq!(net.transitions[0].outputs[0].weight, 1);
    }

    #[test]
    fn lexer_reports_line_and_column() {
        // 第二行放一个非法字符 @。
        let src = "place p : capacity 1;\nplace q : capacity @1;";
        let err = parse_pnet(src).expect_err("must reject");
        assert_eq!(err.primary_code(), codes::PARSE_ERROR);
        let loc = err.issues[0].location.as_ref().unwrap();
        assert!(loc.starts_with("2:"), "error location must be line 2, got {loc}");
    }

    #[test]
    fn semantic_errors_flow_through_pnet() {
        let src = "place p : capacity 1;\n\
                   transition t: consumes 1 missing; produces;";
        let err = parse_pnet(src).expect_err("unknown place must fail");
        assert_eq!(err.primary_code(), codes::UNKNOWN_PLACE);
    }

    #[test]
    fn weighted_arcs_parse() {
        let src = "place b : capacity 5;\n\
                   transition produce: consumes; produces 2 b;";
        let net = parse_pnet(src).unwrap();
        assert_eq!(net.transitions[0].outputs[0].weight, 2);
        assert_eq!(net.places[0].capacity, 5);
    }
}
