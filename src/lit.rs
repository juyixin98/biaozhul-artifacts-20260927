//! SAT 核心数据类型：变量与（编码后的）文字。
//!
//! 编码约定：变量 v（从 1 开始）的正文字编码为 `2*v`，负文字 `¬v` 编码为 `2*v+1`。
//! 这样 `lit ^ 1` 即得互补文字，`lit >> 1` 即得变量号。0 不是合法文字（与
//! DIMACS 中 0 作为子句终止符一致）。

/// 变量编号，从 1 开始；0 保留。
pub type Var = u32;

/// 编码后的文字。偶数为正文字，奇数为负文字。
pub type Lit = u32;

/// 由带符号 DIMACS 整数构造编码文字，`signed != 0`。
#[inline]
pub fn lit_from_signed(signed: i64) -> Lit {
    debug_assert!(signed != 0, "0 是子句终止符，不是文字");
    let v = signed.unsigned_abs() as Lit;
    if signed > 0 {
        2 * v
    } else {
        2 * v + 1
    }
}

/// 还原为带符号 DIMACS 整数（正变量号 / 负变量号）。
#[inline]
pub fn lit_to_signed(lit: Lit) -> i64 {
    let v = (lit >> 1) as i64;
    if lit & 1 == 0 {
        v
    } else {
        -v
    }
}

/// 文字所属变量（1-based）。
#[inline]
pub fn var_of(lit: Lit) -> Var {
    lit >> 1
}

/// 互补文字：p ↔ ¬p。
#[inline]
pub fn neg(lit: Lit) -> Lit {
    lit ^ 1
}

/// 文字极性：true 为正文字。
#[inline]
pub fn sign_positive(lit: Lit) -> bool {
    lit & 1 == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signed_roundtrip_and_negation() {
        for s in [-12i64, -1, 1, 7, 300] {
            let l = lit_from_signed(s);
            assert_eq!(lit_to_signed(l), s);
            assert_eq!(lit_to_signed(neg(l)), -s);
            assert_eq!(var_of(l), s.unsigned_abs() as u32);
            assert_eq!(neg(neg(l)), l);
        }
        assert!(sign_positive(lit_from_signed(1)));
        assert!(!sign_positive(lit_from_signed(-1)));
    }
}
