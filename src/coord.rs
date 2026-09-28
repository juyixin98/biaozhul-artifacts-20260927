//! 离线坐标压缩。
//!
//! 建表（注册）时一次性给出全部合法坐标；之后每个维度的顺序**冻结**：
//! 排序、去重，升序固定。未注册坐标的更新一律拒绝（见 [`store::Store`]），
//! 绝不会被插入到某个“就近”位置。

use crate::error::{CoreError, CoreResult};

/// 一个维度的冻结坐标表：升序、唯一。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Axis {
    values: Vec<i64>,
}

impl Axis {
    /// 排序 + 去重建轴，返回 (轴, 输入中被去掉的重复条目数)。
    /// 空列表报错（轴上必须至少有一个坐标）。
    pub fn from_coords(mut raw: Vec<i64>, which: &'static str) -> CoreResult<(Axis, usize)> {
        if raw.is_empty() {
            return Err(CoreError::EmptyCoordinates(which));
        }
        let input_len = raw.len();
        raw.sort_unstable();
        raw.dedup();
        let duplicates = input_len - raw.len();
        Ok((Axis { values: raw }, duplicates))
    }

    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    /// 精确查找：坐标必须已注册，返回其 0-based 压缩下标；否则 None。
    pub fn index_of(&self, v: i64) -> Option<usize> {
        self.values.binary_search(&v).ok()
    }

    /// `<= v` 的已注册坐标个数（即前缀求和使用的 1-based 长度）。
    /// 没有任何坐标 <= v 时返回 0。
    pub fn rank_leq(&self, v: i64) -> usize {
        // partition_point 返回第一个使 predicate 为 false 的下标，
        // 即严格大于 v 的位置；对 i64::MIN/MAX 无算术溢出风险。
        self.values.partition_point(|c| *c <= v)
    }

    /// `< v` 的已注册坐标个数（闭区间下界映射用，避免 `v - 1` 在 i64::MIN 溢出）。
    pub fn rank_lt(&self, v: i64) -> usize {
        self.values.partition_point(|c| *c < v)
    }

    pub fn values(&self) -> &[i64] {
        &self.values
    }
}

/// 二维冻结坐标表。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoordinateTable {
    pub xs: Axis,
    pub ys: Axis,
}

/// 注册结果统计（重复坐标被去重的条目数，供测试断言）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DedupStats {
    pub duplicate_x: usize,
    pub duplicate_y: usize,
}

impl CoordinateTable {
    /// 固定顺序：先按 x 升序去重，再按 y 升序去重。
    /// 两轴独立压缩；矩形查询是两轴选择结果的笛卡尔积。
    pub fn from_coords(
        raw_x: Vec<i64>,
        raw_y: Vec<i64>,
    ) -> CoreResult<(CoordinateTable, DedupStats)> {
        let (xs, duplicate_x) = Axis::from_coords(raw_x, "x")?;
        let (ys, duplicate_y) = Axis::from_coords(raw_y, "y")?;
        Ok((
            CoordinateTable { xs, ys },
            DedupStats {
                duplicate_x,
                duplicate_y,
            },
        ))
    }

    pub fn nx(&self) -> usize {
        self.xs.len()
    }

    pub fn ny(&self) -> usize {
        self.ys.len()
    }

    /// 精确映射点到压缩格点。任一维未注册即返回 None（调用方负责拒绝）。
    pub fn cell_of(&self, x: i64, y: i64) -> Option<(usize, usize)> {
        Some((self.xs.index_of(x)?, self.ys.index_of(y)?))
    }

    /// 一维上 `<= bound` 的压缩下标个数（Fenwick 前缀长度）。
    pub fn rank_x_leq(&self, bound: i64) -> usize {
        self.xs.rank_leq(bound)
    }

    pub fn rank_y_leq(&self, bound: i64) -> usize {
        self.ys.rank_leq(bound)
    }

    /// 一维上 `< bound` 的压缩下标个数（闭区间下界）。
    pub fn rank_x_lt(&self, bound: i64) -> usize {
        self.xs.rank_lt(bound)
    }

    pub fn rank_y_lt(&self, bound: i64) -> usize {
        self.ys.rank_lt(bound)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dedup_and_fixed_order() {
        let (t, stats) = CoordinateTable::from_coords(vec![3, 1, 3, -7], vec![9, 9, 2]).unwrap();
        assert_eq!(
            stats,
            DedupStats {
                duplicate_x: 1,
                duplicate_y: 1
            }
        );
        assert_eq!(t.xs.values(), &[-7, 1, 3]);
        assert_eq!(t.ys.values(), &[2, 9]);
        assert_eq!(t.cell_of(3, 9), Some((2, 1)));
        assert_eq!(t.cell_of(4, 9), None);
        assert_eq!(t.cell_of(3, 8), None);
        // 极端边界：落在坐标之间、坐标之外。
        assert_eq!(t.rank_x_leq(i64::MIN), 0);
        assert_eq!(t.rank_x_leq(-8), 0);
        assert_eq!(t.rank_x_leq(-7), 1);
        assert_eq!(t.rank_x_leq(0), 1);
        assert_eq!(t.rank_x_leq(i64::MAX), 3);
        assert_eq!(t.rank_y_leq(1), 0);
        assert_eq!(t.rank_y_leq(9), 2);
    }

    #[test]
    fn empty_axes_rejected() {
        assert_eq!(
            CoordinateTable::from_coords(vec![], vec![1])
                .unwrap_err()
                .code(),
            "EMPTY_COORDINATES"
        );
        assert_eq!(
            CoordinateTable::from_coords(vec![1], vec![])
                .unwrap_err()
                .code(),
            "EMPTY_COORDINATES"
        );
    }
}
