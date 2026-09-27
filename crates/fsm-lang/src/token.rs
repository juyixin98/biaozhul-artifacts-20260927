/// Lexical token. `pos` is a 0-based byte offset into the source text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Token {
    pub kind: TokenKind,
    pub pos: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TokenKind {
    // literals / names
    Ident(String),
    Int(i64),
    // keywords
    KwSystem,
    KwVar,
    KwBool,
    KwInt,
    KwEnum,
    KwInit,
    KwTransition,
    KwGuard,
    KwThen,
    KwTerminal,
    KwTrue,
    KwFalse,
    KwIf,
    KwElse,
    KwNot,
    KwMod,
    // punctuation / operators
    LParen,
    RParen,
    LBrace,
    RBrace,
    LBracket,
    RBracket,
    Comma,
    Colon,
    Semicolon,
    Assign, // :=
    DotDot, // ..
    Dot,
    Plus,
    Minus,
    Star,
    Slash,
    Lt,
    Le,
    Gt,
    Ge,
    EqEq,
    Neq,
    And,
    Or,
    Bang,
    Eof,
}
