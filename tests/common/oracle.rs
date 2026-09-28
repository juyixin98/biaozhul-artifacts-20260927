//! 独立参考实现：稀疏映射全扫描。
//!
//! 与 pr2d 内核零共享逻辑：
//! - 注册集合用 `BTreeSet`（测试自己的排序，不调 `coord::Axis`）；
//! - 每版本保存整份 `HashMap` 点值（i128），模拟 MVCC 历史；
//! - 矩形和直接遍历点表全扫描，闭区间判断；
//! - 逐点溢出规则独立实现（i128 累计，超出 i64 判溢出）。

use std::collections::{BTreeSet, HashMap};

use pr2d::model::PointUpdate;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OracleReject {
    pub code: &'static str,
    pub detail: String,
}

/// 单个版本的点值状态（版本 0 为空基线）。
type Points = HashMap<(i64, i64), i128>;

pub struct Oracle {
    xs: BTreeSet<i64>,
    ys: BTreeSet<i64>,
    /// 下标即版本号。
    history: Vec<Points>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OracleRect {
    pub x_lo: i64,
    pub x_hi: i64,
    pub y_lo: i64,
    pub y_hi: i64,
}

impl Oracle {
    pub fn new(xs: Vec<i64>, ys: Vec<i64>) -> Result<Oracle, OracleReject> {
        if xs.is_empty() {
            return Err(OracleReject {
                code: "EMPTY_COORDINATES",
                detail: "x".into(),
            });
        }
        if ys.is_empty() {
            return Err(OracleReject {
                code: "EMPTY_COORDINATES",
                detail: "y".into(),
            });
        }
        Ok(Oracle {
            xs: xs.into_iter().collect(),
            ys: ys.into_iter().collect(),
            history: vec![HashMap::new()],
        })
    }

    pub fn latest_version(&self) -> u64 {
        (self.history.len() - 1) as u64
    }

    /// 参考提交：返回新版本号；拒绝类别与内核对齐，但由独立路径得出。
    pub fn commit(&mut self, updates: &[PointUpdate]) -> Result<u64, OracleReject> {
        if updates.is_empty() {
            return Err(OracleReject {
                code: "EMPTY_BATCH",
                detail: "empty".into(),
            });
        }
        // 1) 全部点必须已注册
        for u in updates {
            if !self.xs.contains(&u.x) || !self.ys.contains(&u.y) {
                return Err(OracleReject {
                    code: "COORDINATE_NOT_REGISTERED",
                    detail: format!("({}, {})", u.x, u.y),
                });
            }
        }
        // 2) 批内聚合 + 逐点溢出检查（独立 i128 路径）
        let mut agg: HashMap<(i64, i64), i128> = HashMap::new();
        for u in updates {
            *agg.entry((u.x, u.y)).or_insert(0) += u.delta as i128;
        }
        let current = self.history.last().unwrap();
        let mut next = current.clone();
        for ((x, y), d) in agg {
            let prev = next.get(&(x, y)).copied().unwrap_or(0);
            let nv = prev + d;
            if nv < i64::MIN as i128 || nv > i64::MAX as i128 {
                return Err(OracleReject {
                    code: "POINT_OVERFLOW",
                    detail: format!("({x}, {y}) prev={prev} batch_delta={d}"),
                });
            }
            next.insert((x, y), nv);
        }
        self.history.push(next);
        Ok(self.latest_version())
    }

    /// 全扫描矩形和（闭区间），返回 (sum, 是否选到至少一个已注册坐标)。
    pub fn query(&self, version: u64, r: OracleRect) -> Result<(i128, bool), OracleReject> {
        if r.x_lo > r.x_hi || r.y_lo > r.y_hi {
            return Err(OracleReject {
                code: "INVERTED_RECT",
                detail: "inverted".into(),
            });
        }
        let snap = self
            .history
            .get(version as usize)
            .ok_or_else(|| OracleReject {
                code: "VERSION_NOT_FOUND",
                detail: format!("v{version}"),
            })?;
        let x_sel: BTreeSet<i64> = self.xs.range(r.x_lo..=r.x_hi).copied().collect();
        let y_sel: BTreeSet<i64> = self.ys.range(r.y_lo..=r.y_hi).copied().collect();
        let non_empty = !x_sel.is_empty() && !y_sel.is_empty();
        let mut sum: i128 = 0;
        for ((x, y), v) in snap {
            if x_sel.contains(x) && y_sel.contains(y) {
                sum += *v;
            }
        }
        Ok((sum, non_empty))
    }

    pub fn xs(&self) -> Vec<i64> {
        self.xs.iter().copied().collect()
    }

    pub fn ys(&self) -> Vec<i64> {
        self.ys.iter().copied().collect()
    }
}
