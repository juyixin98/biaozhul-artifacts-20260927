pub mod interval;
pub mod solver;
pub mod state;

pub use interval::{Bound, Interval, OverflowClass, I64_MAX, I64_MIN};
pub use solver::{validate, Analyzer};
pub use state::{AbsState, ArrayAbs};
