//! Source locations and spans used throughout the AST and diagnostics.
use serde::{Deserialize, Serialize};

/// Byte offset and 1-based line/column in the source text.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Loc {
    pub offset: usize,
    pub line: u32,
    pub column: u32,
}

/// Half-open byte span `[start, end)` with resolved locations at both ends.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Span {
    pub start: Loc,
    pub end: Loc,
}

impl Span {
    pub fn new(start: Loc, end: Loc) -> Self {
        Self { start, end }
    }

    pub fn point(loc: Loc) -> Self {
        Self { start: loc, end: loc }
    }

    pub fn merge(self, other: Span) -> Span {
        Span {
            start: self.start,
            end: other.end,
        }
    }
}

/// Resolve a byte offset into 1-based line/column using the full source.
pub fn loc_at(source: &str, offset: usize) -> Loc {
    let mut line = 1u32;
    let mut col = 1u32;
    for (i, ch) in source.char_indices() {
        if i == offset {
            break;
        }
        if ch == '\n' {
            line += 1;
            col = 1;
        } else {
            col += 1;
        }
    }
    Loc {
        offset,
        line,
        column: col,
    }
}
