//! P 不变量（place invariants）计算与候选验证支撑。
//!
//! 方法（精确整数/有理数运算，不使用浮点）：
//! 1. 构造关联矩阵 `C[p,t] = 输出权 - 输入权`；
//! 2. 在有理数域上做 Gauss-Jordan 消元求右零空间 `C·y = 0` 的基；
//! 3. 把有理基向量化为本原整数向量（乘分母最小公倍数后除以最大公约数）；
//! 4. 在有界范围内枚举基向量的整数组合，保留非零且分量全非负者作为 **P 不变量候选**，
//!    归一化去重、按范数排序返回。
//!
//! 边界枚举是完备性受限的：小整数组合之外可能存在更大系数的不变量，
//! 报告里以 `candidates_truncated` 显式标注；这与容量模型下可达判定的边界一致。

use serde::Serialize;

use super::model::Net;

/// 一个非负 P 不变量候选：权向量 y ≥ 0、y ≠ 0、C·y = 0。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PInvariant {
    /// 按库所顺序排列的非负权。
    pub weights: Vec<i64>,
    pub support_size: usize,
    pub l1_norm: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PInvariantReport {
    /// 关联矩阵在有理数域上的秩。
    pub incidence_rank: usize,
    /// 右零空间维数 = 库所数 - 秩；为 0 时不存在任何 P 不变量（也不存在守恒律）。
    pub nullspace_dimension: usize,
    /// 有界枚举是否因组合数上限被截断。
    pub candidates_truncated: bool,
    /// 非负 P 不变量候选（归一化、去重、按 l1 范数排序）。
    pub candidates: Vec<PInvariant>,
    /// 零空间的本原整数基（允许负分量；任何线性守恒律都是它们的有理组合）。
    pub signed_basis: Vec<Vec<i64>>,
    /// 计算方法与完备性边界说明，供响应与日志直接引用。
    pub method: String,
}

/// 枚举时允许的系数 l1 范数上界，以及组合元组总数安全阀。
#[derive(Debug, Clone, Copy)]
pub struct InvariantBounds {
    pub coefficient_bound: i64,
    pub max_combinations: usize,
    pub max_candidates: usize,
}

impl Default for InvariantBounds {
    fn default() -> Self {
        InvariantBounds {
            coefficient_bound: 4,
            max_combinations: 2_000_000,
            max_candidates: 256,
        }
    }
}

/// 精确有理数（den 恒正，运算后归一化）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct F {
    num: i128,
    den: i128,
}

impl F {
    fn from_i(v: i128) -> Self {
        F { num: v, den: 1 }
    }

    fn is_zero(&self) -> bool {
        self.num == 0
    }

    fn normalize(self) -> Self {
        if self.num == 0 {
            return F { num: 0, den: 1 };
        }
        let g = gcd(self.num.unsigned_abs(), self.den.unsigned_abs()) as i128;
        let (num, den) = (self.num / g, self.den / g);
        if den < 0 {
            F {
                num: -num,
                den: -den,
            }
        } else {
            F {
                num,
                den,
            }
        }
    }

    fn sub(self, o: Self) -> Self {
        F {
            num: self.num * o.den - o.num * self.den,
            den: self.den * o.den,
        }
        .normalize()
    }

    fn mul(self, o: Self) -> Self {
        F {
            num: self.num * o.num,
            den: self.den * o.den,
        }
        .normalize()
    }

    fn div(self, o: Self) -> Self {
        F {
            num: self.num * o.den,
            den: self.den * o.num,
        }
        .normalize()
    }
}

fn gcd(a: u128, b: u128) -> u128 {
    if b == 0 {
        a
    } else {
        gcd(b, a % b)
    }
}

/// 关联矩阵 C[p,t] = out(p,t) - in(p,t)。
pub fn incidence_matrix(net: &Net) -> Vec<Vec<i64>> {
    let (p, t) = (net.place_count(), net.transition_count());
    let mut c = vec![vec![0i64; t]; p];
    for (ti, tr) in net.transitions.iter().enumerate() {
        for arc in &tr.inputs {
            c[arc.place][ti] -= arc.weight;
        }
        for arc in &tr.outputs {
            c[arc.place][ti] += arc.weight;
        }
    }
    c
}

/// 在有理数域上求 C·y = 0 的本原整数基（允许负分量）。
#[allow(clippy::needless_range_loop)] // RREF 消元按列索引两行，范围循环最直接
fn nullspace_integer_basis(net: &Net) -> (usize, Vec<Vec<i64>>) {
    let c = incidence_matrix(net);
    let p = net.place_count();
    let t = net.transition_count();

    // 方程行：每个变迁一行（C 的转置），列为库所。
    let mut a: Vec<Vec<F>> = (0..t)
        .map(|ti| (0..p).map(|pi| F::from_i(i128::from(c[pi][ti]))).collect())
        .collect();

    let mut pivot_row = 0usize;
    let mut pivot_cols: Vec<usize> = Vec::new();
    if t > 0 {
        for col in 0..p {
            let Some(sel) = (pivot_row..t).find(|&r| !a[r][col].is_zero()) else {
                continue;
            };
            a.swap(pivot_row, sel);
            let piv = a[pivot_row][col];
            for entry in a[pivot_row].iter_mut() {
                *entry = entry.div(piv);
            }
            for r in 0..t {
                if r != pivot_row && !a[r][col].is_zero() {
                    let factor = a[r][col];
                    let pivot = pivot_row;
                    for j in 0..p {
                        let v = a[r][j].sub(factor.mul(a[pivot][j]));
                        a[r][j] = v;
                    }
                }
            }
            pivot_cols.push(col);
            pivot_row += 1;
            if pivot_row == t {
                break;
            }
        }
    }

    let rank = pivot_cols.len();
    let free: Vec<usize> = (0..p).filter(|c| !pivot_cols.contains(c)).collect();

    let mut basis = Vec::with_capacity(free.len());
    for f in &free {
        // 有理向量：自由变量取 1，主元变量取 -RREF 行对应列的值。
        let mut rational = vec![F::from_i(0); p];
        rational[*f] = F::from_i(1);
        for (r, &pc) in pivot_cols.iter().enumerate() {
            rational[pc] = F::from_i(0).sub(a[r][*f]);
        }
        basis.push(to_primitive_integer(&rational));
    }
    (rank, basis)
}

/// 有理向量 -> 本原整数向量（乘分母 LCM、除 GCD、首项非零为正）。
fn to_primitive_integer(v: &[F]) -> Vec<i64> {
    let lcm = v
        .iter()
        .fold(1i128, |acc, x| acc / gcd(acc.unsigned_abs(), x.den.unsigned_abs()) as i128 * x.den);
    let ints: Vec<i128> = v.iter().map(|x| x.num * (lcm / x.den)).collect();
    let g = ints
        .iter()
        .fold(0u128, |acc, &x| gcd(acc, x.unsigned_abs())) as i128;
    let g = g.max(1);
    let mut out: Vec<i64> = ints.iter().map(|x| (x / g) as i64).collect();
    if let Some(first) = out.iter().find(|&&x| x != 0) {
        if *first < 0 {
            for x in &mut out {
                *x = -*x;
            }
        }
    }
    out
}

/// 整数向量各分量绝对值的 gcd。
fn vec_gcd(v: &[i128]) -> u128 {
    v.iter()
        .fold(0u128, |acc, &x| gcd(acc, x.unsigned_abs()))
}

/// 有界枚举非负 P 不变量候选。
pub fn compute_invariants(net: &Net, bounds: InvariantBounds) -> PInvariantReport {
    let (rank, signed_basis) = nullspace_integer_basis(net);
    let p = net.place_count();
    let k = signed_basis.len();
    let b = bounds.coefficient_bound;

    let mut candidates: Vec<Vec<i128>> = Vec::new();
    let mut seen: Vec<Vec<i128>> = Vec::new();
    let mut truncated = false;
    let mut combos: usize = 0;

    if k > 0 {
        // 深度优先枚举满足 sum|c_i| <= b 的整数系数组合。
        let mut coeffs = vec![0i64; k];
        enumerate(
            &signed_basis,
            &mut coeffs,
            0,
            b,
            &mut combos,
            bounds.max_combinations,
            &mut |v: Vec<i128>| {
                if candidates.len() >= bounds.max_candidates {
                    truncated = true;
                    return;
                }
                if v.iter().all(|&x| x >= 0) && v.iter().any(|&x| x > 0) {
                    let g = vec_gcd(&v).max(1) as i128;
                    let norm: Vec<i128> = v.iter().map(|x| x / g).collect();
                    if !seen.contains(&norm) {
                        seen.push(norm.clone());
                        candidates.push(norm);
                    }
                }
            },
        );
        if combos >= bounds.max_combinations {
            truncated = true;
        }
    }

    candidates.sort_by(|a, c| {
        let la: i128 = a.iter().sum();
        let lc: i128 = c.iter().sum();
        la.cmp(&lc).then_with(|| a.cmp(c))
    });

    let candidates = candidates
        .into_iter()
        .map(|v| {
            let weights: Vec<i64> = v.iter().map(|&x| x as i64).collect();
            let support_size = weights.iter().filter(|&&x| x > 0).count();
            let l1_norm = weights.iter().sum();
            PInvariant {
                weights,
                support_size,
                l1_norm,
            }
        })
        .collect();

    PInvariantReport {
        incidence_rank: rank,
        nullspace_dimension: k,
        candidates_truncated: truncated,
        candidates,
        signed_basis,
        method: format!(
            "rational RREF nullspace (rank {rank}/{p}), bounded enumeration of integer \
             combinations with |coeff|_1 <= {b}; candidates outside the bound are not excluded, \
             so a short candidate list is not a proof of absence beyond the bound"
        ),
    }
}

#[allow(clippy::too_many_arguments)]
fn enumerate<F: FnMut(Vec<i128>)>(
    basis: &[Vec<i64>],
    coeffs: &mut [i64],
    idx: usize,
    budget: i64,
    combos: &mut usize,
    max_combos: usize,
    emit: &mut F,
) {
    if *combos >= max_combos {
        return;
    }
    if idx == coeffs.len() {
        *combos += 1;
        // v = Σ c_i · basis_i（i128 检查加法）。
        let p = basis[0].len();
        let mut v = vec![0i128; p];
        for (ci, bvec) in coeffs.iter().zip(basis.iter()) {
            if *ci == 0 {
                continue;
            }
            for (j, &e) in bvec.iter().enumerate() {
                let Some(add) = (i128::from(*ci)).checked_mul(i128::from(e)) else {
                    return; // 溢出的组合直接跳过，不计为候选
                };
                let Some(s) = v[j].checked_add(add) else {
                    return;
                };
                v[j] = s;
            }
        }
        emit(v);
        return;
    }
    // c_idx ∈ [-budget, budget]，其余系数的剩余预算 = budget - |c_idx|。
    for c in -budget..=budget {
        coeffs[idx] = c;
        let rest = budget - c.unsigned_abs() as i64;
        enumerate(
            basis,
            coeffs,
            idx + 1,
            rest,
            combos,
            max_combos,
            emit,
        );
        if *combos >= max_combos {
            return;
        }
    }
}

/// 判断给定（可能带符号的）权向量是否是整数守恒律：C·y = 0。
/// 返回残差向量（按变迁顺序），全 0 即合法。
pub fn conservation_residual(net: &Net, weights: &[i64]) -> Vec<i128> {
    net.transitions
        .iter()
        .map(|tr| {
            let mut r: i128 = 0;
            for arc in &tr.inputs {
                r += i128::from(weights[arc.place]) * i128::from(arc.weight);
            }
            for arc in &tr.outputs {
                r -= i128::from(weights[arc.place]) * i128::from(arc.weight);
            }
            r
        })
        .collect()
}

/// 权向量在某标识上的加权令牌和。
pub fn weighted_sum(weights: &[i64], marking: &[i64]) -> i128 {
    weights
        .iter()
        .zip(marking.iter())
        .map(|(&w, &m)| i128::from(w) * i128::from(m))
        .sum()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::kernel::model::{ArcExpr, Marking, Net, Place, Transition};

    fn places(names: &[(&str, i64)]) -> Vec<Place> {
        names
            .iter()
            .map(|&(n, c)| Place {
                name: n.into(),
                capacity: c,
            })
            .collect()
    }

    #[test]
    fn nullspace_and_invariant_for_two_place_conservative_net() {
        // p0 --t--> p1，C^T·y=0 要求 y0=y1，非负不变量 [1,1]。
        let net = Net {
            places: places(&[("p0", 3), ("p1", 3)]),
            transitions: vec![Transition {
                name: "t".into(),
                inputs: vec![ArcExpr { place: 0, weight: 1 }],
                outputs: vec![ArcExpr { place: 1, weight: 1 }],
            }],
        };
        let rep = compute_invariants(&net, InvariantBounds::default());
        assert_eq!(rep.incidence_rank, 1);
        assert_eq!(rep.nullspace_dimension, 1);
        assert!(rep
            .candidates
            .iter()
            .any(|c| c.weights == vec![1, 1]));
        assert_eq!(
            conservation_residual(&net, &[1, 1]),
            vec![0],
            "[1,1] must be a conservation law"
        );
    }

    #[test]
    fn weighted_invariant_respects_arc_weights() {
        // t: 消耗 2 p0，生成 1 p1 => 守恒律 y 满足 2y0 = y1，即 [1,2]。
        let net = Net {
            places: places(&[("p0", 6), ("p1", 6)]),
            transitions: vec![Transition {
                name: "t".into(),
                inputs: vec![ArcExpr { place: 0, weight: 2 }],
                outputs: vec![ArcExpr { place: 1, weight: 1 }],
            }],
        };
        let rep = compute_invariants(&net, InvariantBounds::default());
        assert!(rep
            .candidates
            .iter()
            .any(|c| c.weights == vec![1, 2]), "got {rep:?}");
        // 朴素令牌和不是守恒律（t 会改变总数），加权和才是。
        assert_eq!(weighted_sum(&[1, 2], &Marking(vec![2, 0]).0), 2);
        assert_eq!(weighted_sum(&[1, 2], &Marking(vec![0, 1]).0), 2);
        assert_ne!(conservation_residual(&net, &[1, 1]), vec![0]);
    }

    #[test]
    fn no_invariant_when_nullspace_dimension_zero() {
        // 单库所上自环净 +1：无守恒律。
        let net = Net {
            places: places(&[("p", 4)]),
            transitions: vec![Transition {
                name: "t".into(),
                inputs: vec![ArcExpr { place: 0, weight: 1 }],
                outputs: vec![ArcExpr { place: 0, weight: 2 }],
            }],
        };
        let rep = compute_invariants(&net, InvariantBounds::default());
        assert_eq!(rep.nullspace_dimension, 0);
        assert!(rep.candidates.is_empty());
        assert!(!rep.candidates_truncated);
    }

    #[test]
    fn candidate_verification_rejects_non_conserving_vector() {
        let net = Net {
            places: places(&[("p0", 3), ("p1", 3)]),
            transitions: vec![Transition {
                name: "t".into(),
                inputs: vec![ArcExpr { place: 0, weight: 1 }],
                outputs: vec![ArcExpr { place: 1, weight: 1 }],
            }],
        };
        // [1,0] 不是守恒律，残差非 0。
        let res = conservation_residual(&net, &[1, 0]);
        assert_ne!(res, vec![0]);
    }
}
