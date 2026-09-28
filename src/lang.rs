//! Small input language for difference constraints.
//!
//! One constraint per line:
//!
//! ```text
//! # scheduling: start_b - start_a <= 5
//! c1: x - y <= 5
//! c2: z - x <= -2
//! ```
//!
//! Grammar (line oriented):
//!
//! ```ebnf
//! line      := [comment] | [constraint] ;
//! constraint := ident ':' ident '-' ident '<=' integer ;
//! integer    := ['-'] DIGIT { DIGIT } ;
//! ident      := ( ASCII_ALPHA | '_' ) { ASCII_ALPHANUMERIC | '_' } ;
//! ```
//!
//! Blank lines and `#` comments are ignored. Parse errors carry a 1-based line
//! number and a 1-based column, and are classified as [`ErrorKind::Input`].
//!
//! [`ErrorKind::Input`]: crate::error::ErrorKind::Input

use serde::Serialize;

use crate::error::{ServiceError, ServiceResult};
use crate::model::Constraint;

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub struct LineCol {
    pub line: usize,
    pub column: usize,
}

struct Lexer<'a> {
    line: &'a str,
    line_no: usize,
    pos: usize,
}

impl<'a> Lexer<'a> {
    fn new(line: &'a str, line_no: usize) -> Self {
        Self {
            line,
            line_no,
            pos: 0,
        }
    }

    fn err(&self, msg: impl Into<String>) -> ServiceError {
        ServiceError::input(msg.into()).with_detail(
            serde_json::json!({ "line": self.line_no, "column": self.pos + 1 }),
        )
    }

    fn skip_ws(&mut self) {
        while self.pos < self.line.len() && self.line.as_bytes()[self.pos].is_ascii_whitespace() {
            self.pos += 1;
        }
    }

    fn eof(&self) -> bool {
        self.pos >= self.line.len()
    }

    fn peek(&self) -> Option<u8> {
        self.line.as_bytes().get(self.pos).copied()
    }

    fn consume(&mut self, s: &str) -> bool {
        if self.line[self.pos..].starts_with(s) {
            self.pos += s.len();
            true
        } else {
            false
        }
    }

    fn expect(&mut self, s: &str) -> ServiceResult<()> {
        if self.consume(s) {
            Ok(())
        } else {
            Err(self.err(format!("expected '{s}'")))
        }
    }

    fn ident(&mut self) -> ServiceResult<String> {
        let start = self.pos;
        match self.peek() {
            Some(b) if b.is_ascii_alphabetic() || b == b'_' => self.pos += 1,
            _ => return Err(self.err("expected identifier (letter or underscore)")),
        }
        while let Some(b) = self.peek() {
            if b.is_ascii_alphanumeric() || b == b'_' {
                self.pos += 1;
            } else {
                break;
            }
        }
        Ok(self.line[start..self.pos].to_string())
    }

    fn integer(&mut self) -> ServiceResult<i64> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        let digits_start = self.pos;
        while let Some(b) = self.peek() {
            if b.is_ascii_digit() {
                self.pos += 1;
            } else {
                break;
            }
        }
        if self.pos == digits_start {
            return Err(self.err("expected integer constant"));
        }
        self.line[start..self.pos]
            .parse::<i64>()
            .map_err(|_| self.err("integer constant does not fit in i64"))
    }
}

/// Parse a full document into constraints. Construction goes through
/// [`Constraint::new`], so identifier and value rules are identical to the
/// JSON path.
pub fn parse_constraints(input: &str) -> ServiceResult<Vec<Constraint>> {
    let mut out = Vec::new();
    for (idx, raw_line) in input.lines().enumerate() {
        let line_no = idx + 1;
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // strip trailing inline comment
        let line = match line.find('#') {
            Some(i) => line[..i].trim_end(),
            None => line,
        };
        let mut lx = Lexer::new(line, line_no);
        lx.skip_ws();
        let id = lx.ident()?;
        lx.skip_ws();
        lx.expect(":")?;
        lx.skip_ws();
        let lhs = lx.ident()?;
        lx.skip_ws();
        lx.expect("-")?;
        lx.skip_ws();
        let rhs = lx.ident()?;
        lx.skip_ws();
        lx.expect("<=")?;
        lx.skip_ws();
        let bound = lx.integer()?;
        lx.skip_ws();
        if !lx.eof() {
            return Err(lx.err("unexpected trailing characters; expected end of line"));
        }
        out.push(Constraint::new(id, &lhs, &rhs, bound)?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_document_with_comments_and_blanks() {
        let doc = "\
# header
c1: x - y <= 5

  c2: z-x<=-2   # inline
";
        let cs = parse_constraints(doc).unwrap();
        assert_eq!(cs.len(), 2);
        assert_eq!(cs[0], Constraint::new("c1", "x", "y", 5).unwrap());
        assert_eq!(cs[1], Constraint::new("c2", "z", "x", -2).unwrap());
    }

    #[test]
    fn reports_line_and_column() {
        let bad = "c1: x - y <= 5\nc2: 1x - y <= 1\n";
        let err = parse_constraints(bad).unwrap_err();
        assert_eq!(err.kind, crate::error::ErrorKind::Input);
        let d = err.detail.unwrap();
        assert_eq!(d["line"], 2);
        assert_eq!(d["column"], 5);
    }

    #[test]
    fn rejects_oversized_integer() {
        let err = parse_constraints("c: x - y <= 99999999999999999999999").unwrap_err();
        assert_eq!(err.kind, crate::error::ErrorKind::Input);
    }

    #[test]
    fn rejects_bad_separator() {
        let err = parse_constraints("c: x - y < 1").unwrap_err();
        assert_eq!(err.kind, crate::error::ErrorKind::Input);
        assert!(err.message.contains("'<='"));
    }
}
