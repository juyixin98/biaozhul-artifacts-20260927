//! Fixed-width integer arithmetic.
//!
//! Every value is a `u64` holding an unsigned w-bit bit pattern (w in {8,16,32,64}).
//! Signed operations reinterpret the pattern as a two's-complement `w`-bit integer.
//! Division/remainder edge cases deliberately follow the concrete reference semantics
//! (no panics):
//!
//! * division or remainder by zero evaluates to zero (and is separately reported as a
//!   [`FailureKind::DivByZero`] guard by interpreters/engine),
//! * signed `INT_MIN / -1` wraps (and is separately reported as an
//!   [`FailureKind::Overflow`] guard under [`OverflowMode::Trap`]).

use crate::ast::{BinOp, OverflowMode, UnOp, Width};

/// Reinterpret an unsigned w-bit pattern as a signed value widened to i128.
pub fn to_signed(x: u64, w: Width) -> i128 {
    let sign_bit = 1u64 << (w.bits() - 1);
    if x & sign_bit != 0 {
        // Negative: extend the sign bit.
        let ext = u64::MAX << w.bits();
        ((x | ext) as i64).into()
    } else {
        x as i128
    }
}

/// Wrap an i128 signed result into a w-bit unsigned bit pattern.
pub fn from_signed(x: i128, w: Width) -> u64 {
    let m = w.mask_u64() as u128;
    (x as u128 & m) as u64
}

#[inline]
pub fn mask(x: u64, w: Width) -> u64 {
    x & w.mask_u64()
}

/// Result of an arithmetic evaluation: value plus an optional guard describing a
/// side-condition that must hold on the executing path (divisor non-zero, no overflow).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ArithOutcome {
    pub value: u64,
    /// `true` when a division-by-zero edge case occurred on these concrete operands.
    pub div_by_zero: bool,
    /// `true` when an arithmetic/signed-div overflow occurred on these operands.
    pub overflow: bool,
}

fn mk(value: u64, w: Width, div_by_zero: bool, overflow: bool) -> ArithOutcome {
    ArithOutcome {
        value: mask(value, w),
        div_by_zero,
        overflow,
    }
}

/// Shift amount masked modulo width — matching SMT-LIB `bvshl/bvlshr/bvashr` semantics.
#[inline]
fn shift_amount(x: u64, w: Width) -> u32 {
    (x & ((w.bits() - 1) as u64)) as u32
}

/// Evaluate a binary operation over w-bit concrete values.
#[allow(clippy::manual_checked_ops)]
pub fn eval_bin(op: BinOp, a: u64, b: u64, w: Width, mode: OverflowMode) -> ArithOutcome {
    use BinOp::*;
    let a128 = a as u128;
    let b128 = b as u128;
    let sa = to_signed(a, w);
    let sb = to_signed(b, w);
    let m = w.mask_u64();

    match op {
        Add => {
            let r = a128.wrapping_add(b128);
            let ov = r > m as u128;
            if mode == OverflowMode::Trap && ov {
                ArithOutcome {
                    value: 0,
                    div_by_zero: false,
                    overflow: true,
                }
            } else {
                mk(r as u64, w, false, ov)
            }
        }
        Sub => {
            let r = a128.wrapping_sub(b128);
            // Carry when b > a in the wider representation.
            let ov = b128 > a128;
            if mode == OverflowMode::Trap && ov {
                ArithOutcome {
                    value: 0,
                    div_by_zero: false,
                    overflow: true,
                }
            } else {
                mk(r as u64, w, false, ov)
            }
        }
        Mul => {
            let r = a128.wrapping_mul(b128);
            let ov = r & !(m as u128) != 0;
            if mode == OverflowMode::Trap && ov {
                ArithOutcome {
                    value: 0,
                    div_by_zero: false,
                    overflow: true,
                }
            } else {
                mk(r as u64, w, false, ov)
            }
        }
        Udiv => {
            if b == 0 {
                // Undefined-on-zero; reference returns 0 and flags the guard.
                ArithOutcome {
                    value: 0,
                    div_by_zero: true,
                    overflow: false,
                }
            } else {
                // Divisor known non-zero here (guards are checked separately).
                mk(a / b, w, false, false)
            }
        }
        Urem => {
            if b == 0 {
                ArithOutcome {
                    value: 0,
                    div_by_zero: true,
                    overflow: false,
                }
            } else {
                mk(a % b, w, false, false)
            }
        }
        Sdiv => {
            if sb == 0 {
                ArithOutcome {
                    value: 0,
                    div_by_zero: true,
                    overflow: false,
                }
            } else {
                let min = -(1i128 << (w.bits() - 1));
                let edge = sa == min && sb == -1;
                #[allow(clippy::if_same_then_else)]
                if mode == OverflowMode::Trap && edge {
                    ArithOutcome {
                        value: 0,
                        div_by_zero: false,
                        overflow: true,
                    }
                } else if edge {
                    // Wrap result is INT_MIN itself.
                    ArithOutcome {
                        value: mask(a, w),
                        div_by_zero: false,
                        overflow: true,
                    }
                } else {
                    mk(from_signed(sa / sb, w), w, false, false)
                }
            }
        }
        Srem => {
            if sb == 0 {
                ArithOutcome {
                    value: 0,
                    div_by_zero: true,
                    overflow: false,
                }
            } else {
                let min = -(1i128 << (w.bits() - 1));
                let edge = sa == min && sb == -1;
                #[allow(clippy::if_same_then_else)]
                if mode == OverflowMode::Trap && edge {
                    ArithOutcome {
                        value: 0,
                        div_by_zero: false,
                        overflow: true,
                    }
                } else if edge {
                    ArithOutcome {
                        value: 0,
                        div_by_zero: false,
                        overflow: true,
                    }
                } else {
                    mk(from_signed(sa % sb, w), w, false, false)
                }
            }
        }
        And => mk(a & b, w, false, false),
        Or => mk(a | b, w, false, false),
        Xor => mk(a ^ b, w, false, false),
        Shl => mk(a.wrapping_shl(shift_amount(b, w)), w, false, false),
        LShr => mk(a.wrapping_shr(shift_amount(b, w)), w, false, false),
        AShr => {
            // Arithmetic shift on the signed reinterpretation.
            let n = shift_amount(b, w);
            if n == 0 {
                mk(a, w, false, false)
            } else {
                let r = to_signed(a, w) >> n;
                mk(from_signed(r, w), w, false, false)
            }
        }
        Eq => mk((a == b) as u64, w, false, false),
        Ne => mk((a != b) as u64, w, false, false),
        Ult => mk((a < b) as u64, w, false, false),
        Ule => mk((a <= b) as u64, w, false, false),
        Ugt => mk((a > b) as u64, w, false, false),
        Uge => mk((a >= b) as u64, w, false, false),
        Slt => mk((sa < sb) as u64, w, false, false),
        Sle => mk((sa <= sb) as u64, w, false, false),
        Sgt => mk((sa > sb) as u64, w, false, false),
        Sge => mk((sa >= sb) as u64, w, false, false),
    }
}

/// Evaluate a unary operation over w-bit concrete values.
pub fn eval_un(op: UnOp, a: u64, w: Width, mode: OverflowMode) -> ArithOutcome {
    match op {
        UnOp::Neg => {
            // -INT_MIN overflows signed; as unsigned two's-complement negation it is the
            // same pattern.
            let signed_a = to_signed(a, w);
            let min = -(1i128 << (w.bits() - 1));
            let ov = signed_a == min;
            if mode == OverflowMode::Trap && ov {
                ArithOutcome {
                    value: 0,
                    div_by_zero: false,
                    overflow: true,
                }
            } else {
                mk((0u128.wrapping_sub(a as u128)) as u64, w, false, ov)
            }
        }
        UnOp::Not => mk(!a, w, false, false),
    }
}

/// Truthiness in the language: non-zero is true.
#[inline]
pub fn is_true(v: u64) -> bool {
    v != 0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(op: BinOp, a: u64, b: u64, w: Width) -> u64 {
        eval_bin(op, a, b, w, OverflowMode::Wrap).value
    }

    #[test]
    fn wrap_u8_arith() {
        assert_eq!(v(BinOp::Add, 255, 1, Width::W8), 0);
        assert_eq!(v(BinOp::Sub, 0, 1, Width::W8), 255);
        assert_eq!(v(BinOp::Mul, 16, 16, Width::W8), 0);
        assert!(eval_bin(BinOp::Add, 255, 1, Width::W8, OverflowMode::Wrap).overflow);
    }

    #[test]
    fn trap_u8_add() {
        let r = eval_bin(BinOp::Add, 255, 1, Width::W8, OverflowMode::Trap);
        assert!(r.overflow);
    }

    #[test]
    fn signed_roundtrip_and_ops() {
        assert_eq!(to_signed(255, Width::W8), -1);
        assert_eq!(to_signed(128, Width::W8), -128);
        assert_eq!(from_signed(-1, Width::W8), 255);
        assert_eq!(v(BinOp::Slt, 255, 1, Width::W8), 1); // -1 < 1 signed
        assert_eq!(v(BinOp::Ult, 255, 1, Width::W8), 0); // 255 < 1 unsigned false
        assert_eq!(v(BinOp::Sdiv, 7, 2, Width::W8), 3);
        assert_eq!(v(BinOp::Sdiv, from_signed(-7, Width::W8), 2, Width::W8), from_signed(-3, Width::W8));
    }

    #[test]
    fn shifts_masked() {
        assert_eq!(v(BinOp::Shl, 1, 8, Width::W8), 1); // 8 % 8 == 0
        assert_eq!(v(BinOp::Shl, 1, 3, Width::W8), 8);
        assert_eq!(v(BinOp::AShr, from_signed(-8, Width::W8), 2, Width::W8), from_signed(-2, Width::W8));
        assert_eq!(v(BinOp::LShr, from_signed(-8, Width::W8), 2, Width::W8), 62);
    }

    #[test]
    fn div_by_zero_flagged_not_panicking() {
        let r = eval_bin(BinOp::Udiv, 5, 0, Width::W32, OverflowMode::Wrap);
        assert!(r.div_by_zero);
        assert_eq!(r.value, 0);
    }
}
