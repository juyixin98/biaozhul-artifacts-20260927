//! Lazy value iterators.
//!
//! Iteration never allocates the represented set: both containers yield one
//! value at a time, and the top-level iterator walks chunks in key order.

use crate::container::{Bitmap, Container};
use crate::set::HierBitmap;

/// Yields the 16-bit values of one chunk.
pub enum ContainerValues<'a> {
    /// Sparse: walk the sorted slice.
    Array(std::slice::Iter<'a, u16>),
    /// Dense: walk set bits word by word.
    Bitmap(BitmapIter<'a>),
}

impl<'a> ContainerValues<'a> {
    pub fn new(c: &'a Container) -> Self {
        match c {
            Container::Array(a) => ContainerValues::Array(a.values().iter()),
            Container::Bitmap(b) => ContainerValues::Bitmap(BitmapIter::new(b)),
        }
    }
}

impl Iterator for ContainerValues<'_> {
    type Item = u16;

    fn next(&mut self) -> Option<u16> {
        match self {
            ContainerValues::Array(it) => it.next().copied(),
            ContainerValues::Bitmap(it) => it.next(),
        }
    }
}

/// Streaming iterator over a dense bitmap's set bits.
pub struct BitmapIter<'a> {
    bitmap: &'a Bitmap,
    word: usize,
    bits: u64,
}

impl<'a> BitmapIter<'a> {
    pub fn new(bitmap: &'a Bitmap) -> Self {
        Self {
            bitmap,
            word: 0,
            bits: 0,
        }
    }

    /// Move on until `bits` is non-zero or words are exhausted.
    fn advance(&mut self) {
        while self.bits == 0 && self.word < self.bitmap.words().len() {
            self.bits = self.bitmap.words()[self.word];
            self.word += 1;
        }
    }
}

impl Iterator for BitmapIter<'_> {
    type Item = u16;

    fn next(&mut self) -> Option<u16> {
        self.advance();
        if self.bits == 0 {
            return None;
        }
        // word was already advanced past the active word index.
        let bit = self.bits.trailing_zeros() as usize;
        self.bits &= self.bits - 1;
        Some(((self.word - 1) * 64 + bit) as u16)
    }
}

/// Yields full 32-bit values of a [`HierBitmap`] in ascending order.
pub struct HierIter<'a> {
    chunks: std::collections::btree_map::Iter<'a, u16, Container>,
    current: Option<(u16, ContainerValues<'a>)>,
}

impl<'a> HierIter<'a> {
    pub fn new(set: &'a HierBitmap) -> Self {
        let mut chunks = set.chunks_iter();
        let current = chunks.next().map(|(k, c)| (*k, ContainerValues::new(c)));
        Self { chunks, current }
    }
}

impl Iterator for HierIter<'_> {
    type Item = u32;

    fn next(&mut self) -> Option<u32> {
        loop {
            let (key, it) = self.current.as_mut()?;
            if let Some(low) = it.next() {
                return Some(((*key as u32) << 16) | low as u32);
            }
            self.current = self
                .chunks
                .next()
                .map(|(k, c)| (*k, ContainerValues::new(c)));
        }
    }
}
