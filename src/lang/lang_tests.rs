//! Lexer/parser unit tests, including concrete malformed-input failures.

use super::ast::{BinOp, Expr};
use super::lexer::{lex, Tok};
use super::parse;

fn var(n: &str) -> Expr {
    Expr::Var(n.into())
}

#[test]
fn lexes_all_operators_with_spans() {
    let toks = lex("a && b || c ^ !d -> e <-> true false").unwrap();
    let kinds: Vec<Tok> = toks.iter().map(|t| t.kind).collect();
    assert_eq!(
        kinds,
        vec![
            Tok::Ident,
            Tok::And,
            Tok::Ident,
            Tok::Or,
            Tok::Ident,
            Tok::Xor,
            Tok::Not,
            Tok::Ident,
            Tok::Implies,
            Tok::Ident,
            Tok::Equiv,
            Tok::KwTrue,
            Tok::KwFalse,
            Tok::Eof,
        ]
    );
    // Spans slice the source accurately.
    assert_eq!(
        &"a && b || c ^ !d -> e <-> true false"[toks[1].span.start..toks[1].span.end],
        "&&"
    );
}

#[test]
fn lexer_reports_position_of_bad_character() {
    let err = lex("a & b").unwrap_err();
    assert_eq!(err.pos, 2);
    assert_eq!(err.found, Some('&'));
}

#[test]
fn parser_resolves_operator_precedence() {
    // ! binds tightest, then &&, ||, ^, ->, <->.
    let e = parse("!a && b || c").unwrap();
    // ((!a) && b) || c
    match e {
        Expr::Binary {
            op: BinOp::Or,
            lhs,
            rhs,
        } => {
            assert_eq!(*rhs, var("c"));
            match *lhs {
                Expr::Binary {
                    op: BinOp::And,
                    lhs,
                    rhs,
                } => {
                    assert_eq!(*lhs, Expr::Not(Box::new(var("a"))));
                    assert_eq!(*rhs, var("b"));
                }
                other => panic!("expected && , got {other:?}"),
            }
        }
        other => panic!("expected ||, got {other:?}"),
    }
}

#[test]
fn implication_is_right_associative_and_lower_than_disjunction() {
    let e = parse("a || b -> c -> d").unwrap();
    // (a||b) -> (c -> d)
    match e {
        Expr::Binary {
            op: BinOp::Implies,
            lhs,
            rhs,
        } => {
            assert!(matches!(*lhs, Expr::Binary { op: BinOp::Or, .. }));
            assert!(matches!(
                *rhs,
                Expr::Binary {
                    op: BinOp::Implies,
                    ..
                }
            ));
        }
        other => panic!("expected top-level ->, got {other:?}"),
    }
}

#[test]
fn parentheses_override_precedence() {
    let e = parse("a && (b || c)").unwrap();
    match e {
        Expr::Binary {
            op: BinOp::And,
            rhs,
            ..
        } => {
            assert!(matches!(*rhs, Expr::Binary { op: BinOp::Or, .. }));
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn variables_are_collected_in_first_occurrence_order() {
    let e = parse("z && y || x && z").unwrap();
    assert_eq!(e.variables(), vec!["z", "y", "x"]);
}

#[test]
fn empty_input_is_a_parse_error_with_end_of_input() {
    let err = parse("").unwrap_err();
    assert_eq!(err.found, "end of input");
    assert!(err.expected.contains("true"));
}

#[test]
fn missing_rparen_points_at_the_token() {
    let err = parse("(a && b").unwrap_err();
    assert_eq!(err.found, "end of input");
    assert!(
        err.expected.contains(')'),
        "expected closing paren, got: {}",
        err.expected
    );
}

#[test]
fn dangling_operator_is_rejected_not_panicked() {
    assert!(parse("a && ").is_err());
    assert!(parse("&& a").is_err());
    assert!(parse("a -> ").is_err());
    assert!(parse("!").is_err());
}

#[test]
fn double_ampersand_is_required() {
    assert!(parse("a & b").is_err());
}
