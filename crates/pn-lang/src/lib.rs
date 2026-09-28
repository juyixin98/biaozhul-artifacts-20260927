//! `pn-lang`: input language layer.
//!
//! Parses the `petri-analysis/v1` JSON request language into the typed model
//! of `pn-core`. Parsing failures are classified into three stable categories
//! so callers never have to guess whether a request was malformed bytes, a
//! schema violation or a semantically invalid model: `SYNTAX` means the body
//! is not JSON at all; `SCHEMA` means JSON with the wrong shape, types or
//! required fields; and `SEMANTIC` means well-formed JSON describing an
//! invalid model (unknown place reference, target outside capacity, duplicate
//! names, and similar).

pub mod error;
pub mod input;

pub use error::{InputError, InputErrorCategory, InputErrorReport};
pub use input::{
    parse_analyze_request, AnalysisInput, AnalysisOptions, ArcSpec, NamedTarget, PlaceSpec,
    TransitionSpec, SCHEMA_VERSION,
};
