//! 二维 Fenwick（BIT）索引内核。
//!
//! 压缩网格固定为 `nx × ny`，树以行优先扁平存储，内部索引均为 1-based。
//! 节点累加器使用 **i128**：负更新允许，中间节点即使超出 i64 也不损坏；
//! 仅当最终矩形和无法用 i64 表示时由上层返回 `SUM_OVERFLOW`。
//! 点值本身的 i64 累计溢出在提交前单独检测（见 `store::Store::commit_batch`）。

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fenwick2d {
    nx: usize,
    ny: usize,
    bit: Vec<i128>,
}

#[inline]
fn lowbit(i: usize) -> usize {
    i.isolate_lowest_one()
}

impl Fenwick2d {
    /// 全零树（基线版本）。
    pub fn zeros(nx: usize, ny: usize) -> Fenwick2d {
        Fenwick2d {
            nx,
            ny,
            bit: vec![0; (nx + 1) * (ny + 1)],
        }
    }

    /// 在 0-based 格点 `(ix, iy)` 上累加 `delta`。
    pub fn add(&mut self, ix: usize, iy: usize, delta: i128) {
        debug_assert!(ix < self.nx && iy < self.ny, "cell out of compressed grid");
        let stride = self.ny + 1;
        let mut i = ix + 1;
        while i <= self.nx {
            let mut j = iy + 1;
            while j <= self.ny {
                let idx = i * stride + j;
                self.bit[idx] += delta;
                j += lowbit(j);
            }
            i += lowbit(i);
        }
    }

    /// 前缀和：`ix_len × iy_len` 左上角矩形（均为“<= 某坐标”的压缩长度）。
    /// 入参范围 `[0, nx]` × `[0, ny]`，0 表示该维不取任何坐标（和为 0）。
    pub fn prefix_sum(&self, ix_len: usize, iy_len: usize) -> i128 {
        debug_assert!(ix_len <= self.nx && iy_len <= self.ny);
        let stride = self.ny + 1;
        let mut sum: i128 = 0;
        let mut i = ix_len;
        while i > 0 {
            let mut j = iy_len;
            while j > 0 {
                sum += self.bit[i * stride + j];
                j -= lowbit(j);
            }
            i -= lowbit(i);
        }
        sum
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 与内核无关的稠密直算参考（测试自身手算/独立实现）。
    fn dense_rect_sum(grid: &[Vec<i128>], x0: usize, x1: usize, y0: usize, y1: usize) -> i128 {
        // inclusive 0-based 区间；x0>x1 或 y0>y1 表示空矩形。
        if x0 > x1 || y0 > y1 {
            return 0;
        }
        let mut s = 0;
        for row in grid.iter().take(x1 + 1).skip(x0) {
            for &v in row.iter().take(y1 + 1).skip(y0) {
                s += v;
            }
        }
        s
    }

    #[test]
    fn matches_dense_scan() {
        let (nx, ny) = (4usize, 3usize);
        let mut bit = Fenwick2d::zeros(nx, ny);
        let mut grid = vec![vec![0i128; ny]; nx];
        let adds = [
            (0, 0, 5),
            (0, 0, -2),
            (3, 2, 7),
            (2, 1, -9),
            (2, 1, 4),
            (1, 0, 11),
        ];
        for (x, y, d) in adds {
            bit.add(x, y, d);
            grid[x][y] += d;
        }
        for x0 in 0..nx {
            for x1 in x0..nx {
                for y0 in 0..ny {
                    for y1 in y0..ny {
                        // 前缀包含计数：rank = index+1
                        let s = bit.prefix_sum(x1 + 1, y1 + 1)
                            - bit.prefix_sum(x0, y1 + 1)
                            - bit.prefix_sum(x1 + 1, y0)
                            + bit.prefix_sum(x0, y0);
                        assert_eq!(
                            s,
                            dense_rect_sum(&grid, x0, x1, y0, y1),
                            "rect {x0}..={x1},{y0}..={y1}"
                        );
                    }
                }
            }
        }
        // 空前缀
        assert_eq!(bit.prefix_sum(0, 3), 0);
        assert_eq!(bit.prefix_sum(4, 0), 0);
    }
}
