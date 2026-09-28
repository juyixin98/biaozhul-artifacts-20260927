//! 矩形边界语义。
//!
//! 查询矩形为 **闭区间（inclusive）**：`[x_lo, x_hi] × [y_lo, y_hi]`，
//! 统计的是落在区间内的**已注册格点**上的当前值。
//!
//! - 边界落在两个已注册坐标之间时，按排序位置选择（“<= bound”计数）。
//! - 区间内没有任何已注册坐标时 `empty=true`、`sum=0`（合法查询，不是错误）。
//! - `x_lo > x_hi` 或 `y_lo > y_hi` 是非法请求：`INVERTED_RECT`（400），
//!   不与“空矩形”混淆。

use serde::Deserialize;

use crate::coord::CoordinateTable;
use crate::error::{CoreError, CoreResult};
use crate::fenwick::Fenwick2d;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rect {
    pub x_lo: i64,
    pub x_hi: i64,
    pub y_lo: i64,
    pub y_hi: i64,
}

/// 压缩后的选择结果：两轴上的前缀计数。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Selection {
    /// 选入的 x 压缩下标范围 `[x_start, x_end)`（半开，长度可能为 0）。
    pub x_start: usize,
    pub x_end: usize,
    pub y_start: usize,
    pub y_end: usize,
}

impl Rect {
    pub fn validate(&self) -> CoreResult<()> {
        if self.x_lo > self.x_hi || self.y_lo > self.y_hi {
            return Err(CoreError::InvertedRect {
                x_lo: self.x_lo,
                x_hi: self.x_hi,
                y_lo: self.y_lo,
                y_hi: self.y_hi,
            });
        }
        Ok(())
    }

    /// 将原始坐标边界映射为压缩选择。
    pub fn select(&self, table: &CoordinateTable) -> Selection {
        let x_end = table.rank_x_leq(self.x_hi);
        let x_start = table.rank_x_lt(self.x_lo);
        let y_end = table.rank_y_leq(self.y_hi);
        let y_start = table.rank_y_lt(self.y_lo);
        Selection {
            x_start,
            x_end,
            y_start,
            y_end,
        }
    }
}

impl Selection {
    /// 两轴选择均非空才算“有格点落在矩形内”。
    pub fn is_empty(&self) -> bool {
        self.x_end == self.x_start || self.y_end == self.y_start
    }

    /// 二维容斥求矩形和。`lo` 前缀使用“上界-1”的计数，严格符合闭区间语义。
    pub fn sum(&self, bit: &Fenwick2d) -> i128 {
        let (a, b, c, d) = (self.x_end, self.y_end, self.x_start, self.y_start);
        bit.prefix_sum(a, b) - bit.prefix_sum(c, b) - bit.prefix_sum(a, d) + bit.prefix_sum(c, d)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table() -> CoordinateTable {
        CoordinateTable::from_coords(vec![-100, 0, 100], vec![-7, 7])
            .unwrap()
            .0
    }

    #[test]
    fn boundary_snapping_and_empty() {
        let t = table();
        // 边界夹在坐标之间：[-50, 50] 只选 x=0
        let sel = Rect {
            x_lo: -50,
            x_hi: 50,
            y_lo: -7,
            y_hi: 7,
        }
        .select(&t);
        assert_eq!((sel.x_start, sel.x_end), (1, 2));
        assert!(!sel.is_empty());

        // 完全落在坐标之外：空矩形（合法）
        let sel = Rect {
            x_lo: 101,
            x_hi: 1_000_000,
            y_lo: -7,
            y_hi: 7,
        }
        .select(&t);
        assert!(sel.is_empty());
        let sel = Rect {
            x_lo: -100,
            x_hi: 100,
            y_lo: 8,
            y_hi: 100,
        }
        .select(&t);
        assert!(sel.is_empty());

        // 极端坐标不溢出（x_lo - 1 在 i64::MIN 处）
        let sel = Rect {
            x_lo: i64::MIN,
            x_hi: -101,
            y_lo: i64::MIN,
            y_hi: i64::MAX,
        }
        .select(&t);
        assert!(sel.is_empty());
        let sel = Rect {
            x_lo: i64::MIN,
            x_hi: 0,
            y_lo: i64::MIN,
            y_hi: i64::MAX,
        }
        .select(&t);
        assert_eq!((sel.x_start, sel.x_end), (0, 2));
    }

    #[test]
    fn inverted_is_error_not_empty() {
        let e = Rect {
            x_lo: 2,
            x_hi: 1,
            y_lo: 0,
            y_hi: 1,
        }
        .validate()
        .unwrap_err();
        assert_eq!(e.code(), "INVERTED_RECT");
    }
}
