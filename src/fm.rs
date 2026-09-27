//! FM 索引内核：后向搜索（backward search）、LF 映射、SA 采样定位。
//!
//! # 坐标与语义约定（全服务统一）
//! - 原文 `text` 长度为 m，编码后 `T = text + [哨兵]` 长度为 n = m+1。
//! - 模式匹配位置指模式在原文中的起点，取值 `0..=m`。
//! - **半开区间**：后向搜索返回 `[lo, hi)`，命中数为 `hi - lo`，空区间用 `lo == hi` 表示。
//! - **空模式**：`count = n`（每个后缀都以空前缀开头）；`locate` 返回 `{0,1,…,m}`，
//!   即所有后缀起点（对原文而言是 m+1 个“间隙”位置，含末尾 m）。
//! - **超长模式**（`pattern.len() > m`）：不可能匹配，直接返回空区间；
//!   其行为与“在哨兵截断的文本中搜索”无关，属显式定义，不做隐式搜索。
//!
//! # LF 映射与定位
//! - `LF(i) = C[L[i]] + rank_{L[i]}(i)`，是“FL 的逆”，构成行号上的双射。
//! - 沿 LF 行走同时对应“把原文起点向前移一位”：起点为 p 的行走 k 步后到达起点 p-k
//!   （起点 0 的行再走一步到达哨兵后缀行 sentinel_row，随后停住）。
//! - 采样：对所有满足 `sa[i] % sample_step == 0` 的后缀数组行 i 记录 `(i -> sa[i])`。
//!   任意正文行最多走 `sample_step` 步必入样（p 会递减到 0），故
//!   `原文位置 = 样本sa值 + 行走步数`。

use crate::alphabet::{ALPHABET_SIZE, encode_pattern, encode_text};
use crate::bwt::{build_bwt, build_c};
use crate::error::{FmError, Result};
use crate::rank::Occ;
use crate::suffix_array::build_suffix_array;

/// 后向搜索的半开区间。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Interval {
    pub lo: u64,
    pub hi: u64,
}

impl Interval {
    pub fn is_empty(&self) -> bool {
        self.lo == self.hi
    }
    pub fn count(&self) -> u64 {
        self.hi - self.lo
    }
}

/// 一次后向搜索中“处理单个模式符号”的中间状态，供测试与 verify 接口留痕。
#[derive(Debug, Clone, serde::Serialize)]
pub struct SearchStep {
    /// 本步处理的模式符号（0=哨兵不会出现），从模式末尾向前。
    pub symbol: u16,
    /// 对应正文字节的十六进制（如 "0x61"），哨兵为 null。
    pub byte_hex: Option<String>,
    /// 处理前区间 [lo,hi)。
    pub lo_before: u64,
    pub hi_before: u64,
    /// LF 下界、上界，及处理后区间。
    pub lf_lo: u64,
    pub lf_hi: u64,
    pub lo_after: u64,
    pub hi_after: u64,
    /// 本步之后是否已确定无命中（后续步骤不再发生）。
    pub emptied: bool,
}

/// FM 索引（只读内核）。原文仅在 [`FmIndex::text`] 中保留，供 verify 朴素比对。
#[derive(Debug, Clone)]
pub struct FmIndex {
    /// 编码后长度 n = text.len() + 1。
    n: u64,
    /// rank 结构（内含 BWT 最后一列）。
    occ: Occ,
    /// C 表：符号严格前缀计数。
    c: [u64; ALPHABET_SIZE],
    /// SA 采样：排序的 (后缀数组行号 i, sa[i])，仅含 sa[i] % step == 0 的行。
    sample_rows: Vec<u64>,
    sample_sa: Vec<u32>,
    sample_step: u32,
    /// sa[i]=0（原文起点后缀）所在的 BWT 行，也是哨兵在 BWT 中的唯一位置。
    sentinel_row: u64,
    /// 原始字节文本（建索引输入）。
    text: Vec<u8>,
}

impl FmIndex {
    /// 建立索引。
    ///
    /// - `text` 不能为空（至少 1 字节）；
    /// - `rank_block` 是 rank 检查点块大小（>=1）；
    /// - `sample_step` 是 SA 采样步长（>=1，越小定位越快、采样越大）。
    pub fn build(text: &[u8], rank_block: u32, sample_step: u32) -> Result<Self> {
        if text.is_empty() {
            return Err(FmError::invalid_input("text 不能为空：FM 索引需要非空正文"));
        }
        if rank_block == 0 {
            return Err(FmError::invalid_input("rank_block 必须 >= 1"));
        }
        if sample_step == 0 {
            return Err(FmError::invalid_input("sample_step 必须 >= 1"));
        }
        if text.len() > u32::MAX as usize {
            return Err(FmError::resource_exhausted(format!(
                "文本长度 {} 超出 u32 寻址上限",
                text.len()
            )));
        }

        let syms = encode_text(text);
        let sa = build_suffix_array(&syms);
        let bwt = build_bwt(&syms, &sa);
        let c = build_c(&syms);
        let occ = Occ::new(bwt, rank_block as u64)?;

        let mut sample_rows = Vec::new();
        let mut sample_sa = Vec::new();
        let mut sentinel_row = 0u64;
        for (i, &p) in sa.iter().enumerate() {
            if p == 0 {
                sentinel_row = i as u64;
            }
            if p % sample_step == 0 {
                sample_rows.push(i as u64);
                sample_sa.push(p);
            }
        }

        let idx = FmIndex {
            n: syms.len() as u64,
            occ,
            c,
            sample_rows,
            sample_sa,
            sample_step,
            sentinel_row,
            text: text.to_vec(),
        };
        idx.validate()?;
        Ok(idx)
    }

    /// 供持久化层直接组装各部件（不重建），随后必须通过 [`Self::validate`]。
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn from_parts(
        n: u64,
        occ: Occ,
        c: [u64; ALPHABET_SIZE],
        sample_rows: Vec<u64>,
        sample_sa: Vec<u32>,
        sample_step: u32,
        sentinel_row: u64,
        text: Vec<u8>,
    ) -> Result<Self> {
        let idx = FmIndex {
            n,
            occ,
            c,
            sample_rows,
            sample_sa,
            sample_step,
            sentinel_row,
            text,
        };
        idx.validate()?;
        Ok(idx)
    }

    // ---- 基本访问器（持久化/接口层使用）----

    pub fn text(&self) -> &[u8] {
        &self.text
    }
    pub fn encoded_len(&self) -> u64 {
        self.n
    }
    /// 原文长度 m。
    pub fn text_len(&self) -> u64 {
        self.n - 1
    }
    pub fn rank_block(&self) -> u32 {
        self.occ.block_size() as u32
    }
    pub fn sample_step(&self) -> u32 {
        self.sample_step
    }
    /// sa[i]=0 的后缀行（哨兵在 BWT 中的唯一位置）。
    pub fn sentinel_row(&self) -> u64 {
        self.sentinel_row
    }
    pub fn c_table(&self) -> &[u64; ALPHABET_SIZE] {
        &self.c
    }
    pub(crate) fn occ(&self) -> &Occ {
        &self.occ
    }
    pub(crate) fn sample_pairs(&self) -> Vec<(u64, u32)> {
        self.sample_rows
            .iter()
            .zip(self.sample_sa.iter())
            .map(|(r, s)| (*r, *s))
            .collect()
    }

    // ---- LF 映射 ----

    /// LF(i) = C[L[i]] + rank_{L[i]}(i)。
    #[inline]
    pub fn lf(&self, i: u64) -> u64 {
        let symbol = self.occ.bwt()[i as usize];
        self.c[symbol as usize] + self.occ.rank(symbol, i)
    }

    // ---- 后向搜索 ----

    /// 后向搜索，返回半开区间。空模式返回全集 `[0,n)`，超长模式返回空区间。
    pub fn search(&self, pattern: &[u8]) -> Interval {
        self.search_traced(pattern, false).0
    }

    /// 后向搜索并可要求记录每一步中间状态（`trace=true` 时逐步记录）。
    /// 超长模式提前返回空区间且不产生步骤；空模式直接返回全集、不产生步骤。
    pub fn search_traced(&self, pattern: &[u8], trace: bool) -> (Interval, Vec<SearchStep>) {
        // 空模式：每个后缀都以空前缀开头。
        if pattern.is_empty() {
            return (Interval { lo: 0, hi: self.n }, Vec::new());
        }
        // 显式处理超长模式：不可能出现于任何循环后缀中（不允许跨哨兵匹配）。
        if pattern.len() as u64 > self.n - 1 {
            return (Interval { lo: 0, hi: 0 }, Vec::new());
        }

        let psyms = encode_pattern(pattern);
        let mut iv = Interval { lo: 0, hi: self.n };
        let mut steps = Vec::new();
        // 自右向左消费模式符号。
        for &symbol in psyms.iter().rev() {
            let lo_before = iv.lo;
            let hi_before = iv.hi;
            let lf_lo = self.c[symbol as usize] + self.occ.rank(symbol, iv.lo);
            let lf_hi = self.c[symbol as usize] + self.occ.rank(symbol, iv.hi);
            iv.lo = lf_lo;
            iv.hi = lf_hi;
            if trace {
                steps.push(SearchStep {
                    symbol,
                    byte_hex: Some(format!("0x{:02x}", symbol - 1)),
                    lo_before,
                    hi_before,
                    lf_lo,
                    lf_hi,
                    lo_after: iv.lo,
                    hi_after: iv.hi,
                    emptied: iv.is_empty(),
                });
            }
            if iv.is_empty() {
                break;
            }
        }
        (iv, steps)
    }

    /// 命中数。空模式为 n，超长模式为 0。
    pub fn count(&self, pattern: &[u8]) -> u64 {
        self.search(pattern).count()
    }

    // ---- 定位 ----

    /// 把后缀数组行号解析为原文位置（沿 LF 行走至采样行）。
    fn locate_row(&self, mut row: u64) -> Result<u64> {
        // 哨兵后缀行（sa=n-1）不是合法原文起点；正常查询区间不含该行，
        // 因为任何非空正文模式都不可能匹配只含哨兵的后缀。
        // 从正文起点 0 的采样行再走 LF 会进入 sentinel_row，本循环在那之前即返回。
        for steps in 0u64..=self.sample_step as u64 {
            if let Ok(pos) = self.sample_rows.binary_search(&row) {
                let sa_pos = self.sample_sa[pos] as u64;
                return Ok(sa_pos + steps);
            }
            row = self.lf(row);
        }
        Err(FmError::computation_failed(format!(
            "LF 行走 {} 步仍未命中采样行，索引不变量可能被破坏",
            self.sample_step
        )))
    }

    /// 返回模式全部命中的原文起点（升序、去重由行与位置一一对应保证）。
    /// 空模式返回 0..=m 全集；超长模式返回空。
    pub fn locate(&self, pattern: &[u8]) -> Result<Vec<u64>> {
        let iv = self.search(pattern);
        if pattern.is_empty() {
            // 语义定义：空模式定位到所有后缀起点（含末尾间隙 m）。
            return Ok((0..self.n).collect());
        }
        if iv.is_empty() {
            return Ok(Vec::new());
        }
        let mut out = Vec::with_capacity(iv.count() as usize);
        for row in iv.lo..iv.hi {
            out.push(self.locate_row(row)?);
        }
        out.sort_unstable();
        Ok(out)
    }

    // ---- 不变量校验（建索引与从磁盘加载后都执行）----

    /// 完整校验索引自洽性，任何破坏都报具体错误（corrupt 或 computation_failed）。
    pub fn validate(&self) -> Result<()> {
        if self.n < 2 {
            return Err(FmError::corrupt("编码长度 n 必须 >= 2（非空正文+哨兵）"));
        }
        if self.occ.len() != self.n {
            return Err(FmError::corrupt(format!(
                "BWT 长度 {} 与编码长度 {} 不一致",
                self.occ.len(),
                self.n
            )));
        }
        // C 表单调不减；且对每个符号 s 满足 C[s] + occ(s,n) == C[s+1]
        //（C 是符号严格前缀计数，相邻两项之差恰是符号 s 的总频次）。
        for c in 1..ALPHABET_SIZE {
            if self.c[c] < self.c[c - 1] {
                return Err(FmError::corrupt(format!("C 表在 {c} 处下降")));
            }
        }
        for s in 0..ALPHABET_SIZE as u16 - 1 {
            let freq = self.occ_full_count(s);
            if self.c[s as usize] + freq != self.c[s as usize + 1] {
                return Err(FmError::corrupt(format!(
                    "符号 {s}: C[s]+occ={}+{} 不等于 C[s+1]={}",
                    self.c[s as usize],
                    freq,
                    self.c[s as usize + 1]
                )));
            }
        }
        if self.c[256] + self.occ_full_count(256) != self.n {
            return Err(FmError::corrupt("C[256]+occ(256) 不等于 n"));
        }
        // 哨兵在 BWT 中恰好一次，且位于记录的 sentinel_row（sa[i]=0 行）。
        let sentinel_rows: Vec<u64> = (0..self.n)
            .filter(|&i| self.occ.bwt()[i as usize] == 0)
            .collect();
        if sentinel_rows.len() != 1 {
            return Err(FmError::corrupt(format!(
                "BWT 中哨兵出现 {} 次，期望恰好 1 次",
                sentinel_rows.len()
            )));
        }
        if sentinel_rows[0] != self.sentinel_row {
            return Err(FmError::corrupt(format!(
                "BWT 哨兵实际位于行 {}，与记录的 sentinel_row {} 不符",
                sentinel_rows[0], self.sentinel_row
            )));
        }
        if self.sentinel_row >= self.n {
            return Err(FmError::corrupt("sentinel_row 越界"));
        }
        // LF 必须是 0..n 上的双射（FM 索引基本定理）。
        let mut seen = vec![false; self.n as usize];
        for i in 0..self.n {
            let lf = self.lf(i);
            if lf >= self.n || seen[lf as usize] {
                return Err(FmError::corrupt(format!("LF 在行 {i} 处非双射（lf={lf}）")));
            }
            seen[lf as usize] = true;
        }
        // 采样：行唯一升序、sa 值唯一、全部满足 sa % step == 0。
        if self.sample_rows.len() != self.sample_sa.len() {
            return Err(FmError::corrupt("采样行与采样 SA 值数量不一致"));
        }
        if self.sample_rows.windows(2).any(|w| w[0] >= w[1]) {
            return Err(FmError::corrupt("采样行未严格升序"));
        }
        if self.sample_step == 0 {
            return Err(FmError::corrupt("sample_step 为 0"));
        }
        for (r, p) in self.sample_rows.iter().zip(self.sample_sa.iter()) {
            if *r >= self.n || *p as u64 >= self.n {
                return Err(FmError::corrupt("采样项越界"));
            }
            if *p % self.sample_step != 0 {
                return Err(FmError::corrupt(format!(
                    "采样 SA 值 {p} 不满足步长 {} 整除",
                    self.sample_step
                )));
            }
        }
        // 原文长度与 n 一致。
        if (self.text.len() as u64) != self.n - 1 {
            return Err(FmError::corrupt("原文长度与 n-1 不符"));
        }
        Ok(())
    }

    /// BWT 全列中某符号出现次数。
    fn occ_full_count(&self, symbol: u16) -> u64 {
        self.occ.rank(symbol, self.n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::naive::{naive_count, naive_locate};

    #[test]
    fn banana_known_answers() {
        let idx = FmIndex::build(b"banana", 64, 2).unwrap();
        assert_eq!(idx.count(b"ana"), 2);
        assert_eq!(idx.locate(b"ana").unwrap(), vec![1, 3]); // 重叠匹配：1 与 3
        assert_eq!(idx.count(b"na"), 2);
        assert_eq!(idx.locate(b"na").unwrap(), vec![2, 4]);
        assert_eq!(idx.count(b"a"), 3);
        assert_eq!(idx.locate(b"a").unwrap(), vec![1, 3, 5]);
        assert_eq!(idx.count(b"banana"), 1);
        assert_eq!(idx.locate(b"banana").unwrap(), vec![0]);
        assert_eq!(idx.count(b"xyz"), 0);
        assert_eq!(idx.locate(b"xyz").unwrap(), Vec::<u64>::new());
    }

    #[test]
    fn empty_pattern_semantics() {
        let idx = FmIndex::build(b"abc", 2, 1).unwrap();
        let iv = idx.search(b"");
        assert_eq!(iv, Interval { lo: 0, hi: 4 });
        assert_eq!(idx.count(b""), 4); // n = m+1
        assert_eq!(idx.locate(b"").unwrap(), vec![0, 1, 2, 3]);
    }

    #[test]
    fn longer_than_text_pattern_is_explicit_empty() {
        let idx = FmIndex::build(b"abc", 2, 1).unwrap();
        for p in [b"abcd".as_slice(), b"abcde", b"\x00\x00\x00\x00"] {
            let iv = idx.search(p);
            assert_eq!(iv.count(), 0, "pattern={p:?}");
            assert!(iv.is_empty(), "半开空区间要求 lo==hi, got {:?}", iv);
            assert_eq!(idx.locate(p).unwrap(), Vec::<u64>::new());
        }
        // 长度恰好相等仍可正常搜索
        assert_eq!(idx.count(b"abc"), 1);
    }

    #[test]
    fn build_rejects_empty_text_and_zero_params() {
        assert!(matches!(
            FmIndex::build(b"", 16, 4),
            Err(FmError::InvalidInput { .. })
        ));
        assert!(matches!(
            FmIndex::build(b"x", 0, 4),
            Err(FmError::InvalidInput { .. })
        ));
        assert!(matches!(
            FmIndex::build(b"x", 16, 0),
            Err(FmError::InvalidInput { .. })
        ));
    }

    #[test]
    fn binary_zero_bytes_locate_correctly() {
        let text = b"\x00\x00\xff\x00\x00";
        let idx = FmIndex::build(text, 2, 2).unwrap();
        // 朴素扫描独立核对（窗口起点 0..=3）：
        //   pos0=[00,00] pos1=[00,ff] pos2=[ff,00] pos3=[00,00]
        assert_eq!(idx.count(b"\x00\x00"), 2);
        assert_eq!(idx.locate(b"\x00\x00").unwrap(), vec![0, 3]);
        assert_eq!(idx.count(b"\xff"), 1);
        assert_eq!(idx.locate(b"\xff").unwrap(), vec![2]);
        assert_eq!(idx.count(b"\x00"), 4);
        assert_eq!(idx.locate(b"\x00").unwrap(), vec![0, 1, 3, 4]);
        // 高重叠：连续零串里 000 的起点
        let z = b"\x00\x00\x00\x00";
        let idx2 = FmIndex::build(z, 2, 1).unwrap();
        assert_eq!(idx2.count(b"\x00\x00\x00"), 2);
        assert_eq!(idx2.locate(b"\x00\x00\x00").unwrap(), vec![0, 1]);
    }

    #[test]
    fn sample_steps_all_agree_with_naive() {
        let text = b"aaaaaaabaaaaaaabaaaa"; // 高重复
        for step in 1u32..=8 {
            let idx = FmIndex::build(text, 3, step).unwrap();
            for pat in [
                b"a".as_slice(),
                b"aa",
                b"aaa",
                b"b",
                b"ab",
                b"ba",
                b"aaaaaa",
            ] {
                assert_eq!(
                    idx.count(pat),
                    naive_count(text, pat),
                    "step={step} pat={pat:?}"
                );
                assert_eq!(
                    idx.locate(pat).unwrap(),
                    naive_locate(text, pat),
                    "step={step} pat={pat:?}"
                );
            }
        }
    }
}
