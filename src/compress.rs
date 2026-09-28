//! Coordinate compression that preserves the original value order.
//!
//! Sorted distinct `i64` values map to ranks `0..m` in ascending order, so
//! ordering on the ranks exactly matches ordering on the original values.
//! Signed extremes (`i64::MIN` / `i64::MAX`) and duplicates require no
//! special handling: ordering is decided by ordinary signed comparison and
//! duplicates collapse to one rank.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Compressor {
    /// Sorted, strictly increasing distinct values. Rank `i` maps back to
    /// `values[i]`.
    values: Vec<i64>,
}

impl Compressor {
    pub fn new(data: &[i64]) -> Self {
        let mut values = data.to_vec();
        values.sort_unstable();
        values.dedup();
        Compressor { values }
    }

    /// Restore from an already sorted-and-deduped value table, validating
    /// that it is strictly increasing.
    pub fn from_sorted(values: Vec<i64>) -> Result<Self, String> {
        if values.is_empty() {
            return Err("empty value table".to_string());
        }
        for w in values.windows(2) {
            if w[0] >= w[1] {
                return Err(format!("value table not strictly increasing at {}", w[0]));
            }
        }
        Ok(Compressor { values })
    }

    pub fn values(&self) -> &[i64] {
        &self.values
    }

    pub fn distinct(&self) -> usize {
        self.values.len()
    }

    /// Rank of an existing value.
    pub fn rank_of(&self, v: i64) -> Option<usize> {
        self.values.binary_search(&v).ok()
    }

    /// Original value for a rank produced by this compressor.
    pub fn value_at(&self, rank: usize) -> Option<i64> {
        self.values.get(rank).copied()
    }

    /// Number of distinct values strictly below `bound`. Values with rank in
    /// `0..return_value` are `< bound`.
    pub fn count_distinct_below(&self, bound: i64) -> usize {
        self.values.partition_point(|&v| v < bound)
    }

    /// Compress a sequence into ranks (order-preserving).
    pub fn compress(&self, data: &[i64]) -> Vec<u64> {
        data.iter()
            .map(|&v| self.rank_of(v).expect("all values exist") as u64)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rank_order_matches_value_order_with_extremes() {
        let data = [0, i64::MIN, -1, 42, i64::MAX, i64::MIN, 42];
        let comp = Compressor::new(&data);
        assert_eq!(comp.values(), [i64::MIN, -1, 0, 42, i64::MAX]);
        // Sorted ranks must decode back to the sorted original sequence.
        let ranks: Vec<u64> = data
            .iter()
            .map(|&v| comp.rank_of(v).unwrap() as u64)
            .collect();
        let mut sorted_ranks = ranks.clone();
        sorted_ranks.sort_unstable();
        let decoded: Vec<i64> = sorted_ranks
            .iter()
            .map(|&r| comp.value_at(r as usize).unwrap())
            .collect();
        let mut expected = data.to_vec();
        expected.sort_unstable();
        assert_eq!(decoded, expected);
    }
}
