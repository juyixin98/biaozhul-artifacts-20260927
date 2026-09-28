//! 穷举证据测试（集成测试，独立于内核的参考实现）。
//!
//! 覆盖要求：
//! - 小变量集合（3 变量全部 256 个布尔函数）穷举真值表核验 apply/restrict/flip；
//! - 不同语法同函数、跨管理器变量重命名、GC 后重建；
//! - 输出节点数与独立最小 ROBDD 节点计数逐函数比对；
//! - 不等价见证是具体赋值。
//!
//! 本文件的“标准答案”由以下独立来源产生，均不经过被测内核：
//! - 真值表位掩码枚举；
//! - 测试内自行实现的余因子切分与带补集边最小节点计数 `ref_node_count`；
//! - `Expr::eval` 递归解释器。

use std::collections::{HashMap, HashSet};

use robdd::core::apply::Op;
use robdd::core::{BddError, BddManager};
use robdd::lang::parser::parse;
use robdd::lang::Expr;
use robdd::verify::{equiv_exprs, IdentityMapping, DEFAULT_VAR_CAP};

/// n 变量函数用 `2^n` 位掩码表示（位 k = 第 k 个赋值上的函数值，位 i 对应变量 i）。
type Mask = u16;

/// 在变量 `var`（0 起）上切分：返回宽度减一位的 (低, 高) 余因子掩码。
fn cofactor_split(m: Mask, n: u32, var: u32) -> (Mask, Mask) {
    // 重排：把 var 变成“最高位变量”后取奇偶。先按变量 0 在最低位的
    // 赋值编号重排到以 var 为最低位，再抽偶/奇位。
    let total = 1u32 << n;
    let mut lo = 0u16;
    let mut hi = 0u16;
    let mut out_idx = 0u32;
    for assign in 0..total {
        if (assign >> var) & 1 == 0 {
            let bit = (m >> assign) & 1;
            lo |= bit << out_idx;
            out_idx += 1;
        }
    }
    out_idx = 0;
    for assign in 0..total {
        if (assign >> var) & 1 == 1 {
            let bit = (m >> assign) & 1;
            hi |= bit << out_idx;
            out_idx += 1;
        }
    }
    (lo, hi)
}

/// 独立的“带补集边最小 ROBDD 可达非终节点数”。
///
/// 与内核同一数学约定但实现完全独立（不 import 任何 core 类型）：
/// - 函数 f 与 !f 共享同一物理节点：用 `rep = min(f, !f)` 规范化进入边极性；
/// - 唯一表只存低边无补集的节点：低余因子极性规范化时若翻转过高边，
///   存储的高边要同步翻转；
/// - 物理节点键 = (剩余变量数, 低边规范掩码, 高边存储掩码[含极性])，
///   去重计数即最小可达节点数。
fn ref_node_count(m: Mask, n: u32) -> usize {
    let full = |nv: u32| -> Mask {
        if nv == 0 {
            1
        } else {
            (1u16 << (1u32 << nv)) - 1
        }
    };
    // 返回 (规范掩码, 是否取了补集)。
    let normalize = |x: Mask, nv: u32| -> (Mask, bool) {
        let nx = full(nv) ^ x;
        if x <= nx {
            (x, false)
        } else {
            (nx, true)
        }
    };

    // (剩余变量数, 函数规范掩码)
    let mut memo: HashSet<(u32, Mask)> = HashSet::new();
    // 物理节点键。
    let mut nodes: HashSet<(u32, Mask, Mask)> = HashSet::new();

    fn rec(
        m0: u16,
        n: u32,
        full: &impl Fn(u32) -> u16,
        normalize: &impl Fn(u16, u32) -> (u16, bool),
        memo: &mut HashSet<(u32, u16)>,
        nodes: &mut HashSet<(u32, u16, u16)>,
        split: &impl Fn(u16, u32, u32) -> (u16, u16),
    ) {
        if m0 == 0 || m0 == full(n) {
            return;
        }
        let (r, _par) = normalize(m0, n);
        if !memo.insert((n, r)) {
            return;
        }
        let (lo, hi) = split(r, n, 0);
        if lo == hi {
            // 冗余节点消除：两个余因子相同，顶变量无关，坍缩到子函数。
            rec(lo, n - 1, full, normalize, memo, nodes, split);
            return;
        }
        let (lo_n, par_lo) = normalize(lo, n - 1);
        // 低边取补集规范化时，存储节点的高边同步翻转。
        let hi_stored = if par_lo { full(n - 1) ^ hi } else { hi };
        nodes.insert((n, lo_n, hi_stored));
        // 沿“存储节点”的两条实际子边继续；常量直接到底。
        rec(lo_n, n - 1, full, normalize, memo, nodes, split);
        rec(hi_stored, n - 1, full, normalize, memo, nodes, split);
    }

    rec(
        m,
        n,
        &full,
        &normalize,
        &mut memo,
        &mut nodes,
        &cofactor_split,
    );
    nodes.len()
}

/// 由真值表掩码构造 DNF 表达式（独立来源；仅用语言层 AST）。
fn dnf_expr(m: Mask, vars: &[&str]) -> Expr {
    let n = vars.len() as u32;
    let mut terms = Vec::new();
    for assign in 0..(1u32 << n) {
        if (m >> assign) & 1 == 1 {
            let lits: Vec<Expr> = vars
                .iter()
                .enumerate()
                .map(|(i, name)| {
                    if (assign >> i) & 1 == 1 {
                        Expr::Var((*name).to_string())
                    } else {
                        Expr::Not(Box::new(Expr::Var((*name).to_string())))
                    }
                })
                .collect();
            terms.push(Expr::And(lits));
        }
    }
    if terms.is_empty() {
        Expr::Const(false)
    } else if terms.len() == (1usize << n) {
        Expr::Const(true)
    } else {
        Expr::Or(terms)
    }
}

fn assignment_of(k: u32, n: u32) -> Vec<bool> {
    (0..n).map(|i| (k >> i) & 1 == 1).collect()
}

#[test]
fn exhaustive_3var_every_function_matches_independent_truth_table() {
    let names = vec!["a", "b", "c"];
    let mut mgr =
        BddManager::new(&names.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();

    for f in 0..256u16 {
        let expr = dnf_expr(f, &names);
        // 独立解释器先确认 DNF 表达式确实代表掩码 f。
        for k in 0..8u32 {
            let env: HashMap<String, bool> = names
                .iter()
                .map(|n| n.to_string())
                .zip(assignment_of(k, 3))
                .collect();
            assert_eq!(
                expr.eval(&env),
                (f >> k) & 1 == 1,
                "DNF oracle mismatch f={f} k={k}"
            );
        }
        let edge = mgr
            .build(&expr)
            .unwrap_or_else(|e| panic!("build f={f}: {e}"));
        for k in 0..8u32 {
            let got = mgr.evaluate(edge, &assignment_of(k, 3)).unwrap();
            assert_eq!(got, (f >> k) & 1 == 1, "BDD f={f} k={k}");
        }
    }
}

#[test]
fn exhaustive_3var_apply_all_pairs_all_ops_against_truth_tables() {
    let names = vec!["a", "b", "c"];
    let mut mgr =
        BddManager::new(&names.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();

    // 先建出全部 256 个函数。
    let mut edges = Vec::with_capacity(256);
    for f in 0..256u16 {
        let e = mgr.build(&dnf_expr(f, &names)).unwrap();
        edges.push(e);
    }

    let ops = [Op::And, Op::Or, Op::Xor, Op::Implies, Op::Iff];
    for &op in &ops {
        for f in 0..256u16 {
            for g in 0..256u16 {
                let out = mgr.apply(op, edges[f as usize], edges[g as usize]).unwrap();
                for k in 0..8u32 {
                    let x = (f >> k) & 1 != 0;
                    let y = (g >> k) & 1 != 0;
                    let want = op.eval_const(x, y);
                    let got = mgr.evaluate(out, &assignment_of(k, 3)).unwrap();
                    assert_eq!(got, want, "{op:?} f={f} g={g} k={k}");
                }
            }
        }
    }
}

#[test]
fn exhaustive_3var_canonical_edge_equality_iff_truth_table_equality() {
    let names = vec!["a", "b", "c"];
    let mut mgr =
        BddManager::new(&names.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();
    let mut edges = Vec::with_capacity(256);
    for f in 0..256u16 {
        edges.push(mgr.build(&dnf_expr(f, &names)).unwrap());
    }
    for f in 0..256u16 {
        for g in 0..256u16 {
            assert_eq!(
                edges[f as usize] == edges[g as usize],
                f == g,
                "canonical edge equality must match truth equality: f={f} g={g}"
            );
        }
    }
    // 常量与补集边的具体结论。
    assert_eq!(edges[0].as_value(), Some(false));
    assert_eq!(edges[255].as_value(), Some(true));
    assert_eq!(edges[0], edges[255].flip());
}

#[test]
fn exhaustive_3var_node_counts_match_independent_minimum_reference() {
    let names = vec!["a", "b", "c"];
    let mut mgr =
        BddManager::new(&names.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();

    // 具体函数的独立节点数（手算/参考算法双重来源）。
    // 位 k = a + 2b + 4c。
    let cases: &[(u16, usize, &str)] = &[
        (0x00, 0, "const false"),
        (0xff, 0, "const true"),
        (0x0f, 1, "a"),
        (0x33, 1, "b"),
        (0x55, 1, "c"),
        (0x69, 3, "a xor b xor c (parity)"),
        (0x96, 3, "!(a xor b xor c)"),
    ];
    for (m, want, label) in cases {
        let e = mgr.build(&dnf_expr(*m, &names)).unwrap();
        assert_eq!(
            mgr.reachable_node_count(e).unwrap(),
            *want,
            "{label} mask={m:#06x}"
        );
    }

    // 全部 256 个函数与独立最小节点计数一致。
    for f in 0..256u16 {
        let e = mgr.build(&dnf_expr(f, &names)).unwrap();
        let got = mgr.reachable_node_count(e).unwrap();
        let want = ref_node_count(f, 3);
        assert_eq!(got, want, "node count mismatch f={f:#06x}");
    }
}

#[test]
fn exhaustive_3var_restrict_cofactors_match_independent_tables() {
    let names = vec!["a", "b", "c"];
    let mut mgr =
        BddManager::new(&names.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();

    for f in 0..256u16 {
        let e = mgr.build(&dnf_expr(f, &names)).unwrap();
        for value in [false, true] {
            let out = mgr
                .restrict(e, &HashMap::from([("a".to_string(), value)]))
                .unwrap();
            // 独立地从真值表抽取余因子：a=value 的 4 个赋值连续/间隔重排。
            for k in 0..4u32 {
                // k 的位是 (b,c)；拼回原 3 位赋值编号（a 是第 0 位）。
                let full_k = (k << 1) | (value as u32);
                let want = (f >> full_k) & 1 != 0;
                // 结果图只依赖 b,c，求值时 a 位给默认 false 即可。
                let ordered = vec![false, k & 1 != 0, (k >> 1) & 1 != 0];
                let got = mgr.evaluate(out, &ordered).unwrap();
                assert_eq!(got, want, "restrict f={f} a={value} bc={k}");
            }
        }
    }
}

#[test]
fn different_syntaxes_same_function_share_canonical_edge() {
    let vars = vec!["a".to_string(), "b".to_string(), "c".to_string()];
    let mut mgr = BddManager::new(&vars).unwrap();
    let variants = [
        "!(a & b)",
        "!a | !b",
        "a -> !b",
        "!(a <-> !(!b | false)) | !a", // 绕弯但等价（下面用解释器保证）
    ];
    let mut first_edge = None;
    for text in variants {
        let expr = parse(text).unwrap();
        // 独立解释器确认它确实等于 !(a&b)。
        let target = parse("!(a & b)").unwrap();
        for k in 0..8u32 {
            let env: HashMap<String, bool> = [("a", 0), ("b", 1), ("c", 2)]
                .iter()
                .map(|(n, i)| (n.to_string(), (k >> i) & 1 == 1))
                .collect();
            assert_eq!(
                expr.eval(&env),
                target.eval(&env),
                "syntax {text} not equivalent?"
            );
        }
        let edge = mgr.build(&expr).unwrap();
        if let Some(fe) = first_edge {
            assert_eq!(
                edge, fe,
                "syntax {text} produced a different canonical edge"
            );
        } else {
            first_edge = Some(edge);
        }
    }
}

#[test]
fn renaming_across_managers_is_equivalent_only_under_identity_mapping() {
    // 管理器 A：x,y,z；管理器 B：p,q,r（顺序一致，名字不同）。
    let mut ma = BddManager::new(&["x".into(), "y".into(), "z".into()]).unwrap();
    let mut mb = BddManager::new(&["p".into(), "q".into(), "r".into()]).unwrap();

    let ea = parse("(x & y) | (!x & z)").unwrap();
    let eb = parse("(p & q) | (!p & r)").unwrap();
    let a_edge = ma.build(&ea).unwrap();
    let b_edge = mb.build(&eb).unwrap();

    // 跨管理器边不能混用：具体错误类别。
    assert!(matches!(
        ma.apply(Op::And, a_edge, b_edge),
        Err(BddError::ForeignManager { .. })
    ));

    // 独立真值表在身份映射下判等价（8 个赋值）。
    let mapping = IdentityMapping::new(HashMap::from([
        ("x".to_string(), "p".to_string()),
        ("y".to_string(), "q".to_string()),
        ("z".to_string(), "r".to_string()),
    ]));
    let out = equiv_exprs(&ea, &eb, &mapping, DEFAULT_VAR_CAP).unwrap();
    match out {
        robdd::verify::EquivResult::Equivalent {
            assignments_checked,
            ..
        } => {
            assert_eq!(assignments_checked, 8);
        }
        other => panic!("expected equivalent, got {other:?}"),
    }

    // 错配映射 x->q 不再等价，并给出具体见证。
    let bad_mapping = IdentityMapping::new(HashMap::from([
        ("x".to_string(), "q".to_string()),
        ("y".to_string(), "p".to_string()),
        ("z".to_string(), "r".to_string()),
    ]));
    let out = equiv_exprs(&ea, &eb, &bad_mapping, DEFAULT_VAR_CAP).unwrap();
    match out {
        robdd::verify::EquivResult::NotEquivalent { witness, .. } => {
            // 至少找到一个具体反例赋值。
            let va = ea.eval(&witness.assignment);
            // 右式需按映射重命名后比；这里只断言见证确实区分函数值。
            let renamed = eb.rename(&HashMap::from([
                ("p".to_string(), "y".to_string()), // q->x, p->y
                ("q".to_string(), "x".to_string()),
                ("r".to_string(), "z".to_string()),
            ]));
            let vb = renamed.eval(&witness.assignment);
            assert_ne!(va, vb);
        }
        other => panic!("expected non-equivalent witness, got {other:?}"),
    }
}

#[test]
fn rebuild_after_gc_preserves_roots_and_reproduces_canonical_edges() {
    let mut mgr = BddManager::new(&["a".into(), "b".into(), "c".into()]).unwrap();
    let keep = mgr.build(&parse("(a | b) & c").unwrap()).unwrap();
    mgr.add_root("keep", keep).unwrap();
    let garbage_edges: Vec<_> = ["a ^ b ^ c", "!a & b & !c", "a -> (b <-> c)"]
        .iter()
        .map(|s| mgr.build(&parse(s).unwrap()).unwrap())
        .collect();

    let report = mgr.gc();
    assert_eq!(report.after_nodes, mgr.reachable_node_count(keep).unwrap());
    assert!(report.swept >= 3);

    // 回收后旧边被拒绝（具体错误类别）。
    for g in &garbage_edges {
        assert!(matches!(
            mgr.evaluate(*g, &[false, false, false]),
            Err(BddError::ReclaimedNode { .. })
        ));
    }

    // 根保留并正确求值；重建同函数与等价语法都得到同一条规范边。
    let root = mgr.root("keep").unwrap();
    assert_eq!(root, keep);
    for k in 0..8u32 {
        let want = ((k & 1 != 0) || (k & 2 != 0)) && (k & 4 != 0);
        assert_eq!(mgr.evaluate(root, &assignment_of(k, 3)).unwrap(), want);
    }
    let again = mgr.build(&parse("c & (b | a)").unwrap()).unwrap();
    assert_eq!(again, keep);
}

#[test]
fn independent_oracle_and_kernel_disagree_only_if_kernel_is_broken() {
    // 对 256 个函数的每一对，verify::equiv_exprs（独立解释器）结论必须与
    // “同管理器规范边相等”一致。
    let names = vec!["a", "b", "c"];
    let mut mgr =
        BddManager::new(&names.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();
    let mut edges = vec![];
    let mut exprs = vec![];
    for f in 0..256u16 {
        let e = dnf_expr(f, &names);
        exprs.push(e.clone());
        edges.push(mgr.build(&e).unwrap());
    }
    let identity = IdentityMapping::default();
    for f in 0..256u16 {
        for g in (f as usize)..256 {
            // 常量函数不含变量，equiv_exprs 要求身份映射覆盖两边出现的变量；
            // 任一边为常量时等价性直接由掩码判定。
            let either_const = f == 0 || f == 255 || g == 0 || g == 255;
            let oracle_eq = if either_const {
                f == g as u16
            } else {
                let oracle =
                    equiv_exprs(&exprs[f as usize], &exprs[g], &identity, DEFAULT_VAR_CAP).unwrap();
                matches!(oracle, robdd::verify::EquivResult::Equivalent { .. })
            };
            assert_eq!(edges[f as usize] == edges[g], oracle_eq, "f={f} g={g}");
        }
    }
}
