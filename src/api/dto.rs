//! Request/response data transfer objects for the HTTP API.
//!
//! Kernel [`NodeRef`](crate::kernel::NodeRef)s are passed through opaquely;
//! clients build expressions, hold the returned references, and feed them to
//! later calls. All response bodies carry a `diag` block with the request id.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::kernel::NodeRef;
use crate::verify::EquivReport;

// -- managers ---------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct CreateManagerReq {
    /// Fixed variable order, highest-priority variable first.
    pub order: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct CreateManagerResp {
    pub manager_id: u64,
    pub order: Vec<String>,
    #[serde(flatten)]
    pub envelope: Envelope,
}

#[derive(Debug, Serialize)]
pub struct ManagerInfoResp {
    pub manager_id: u64,
    pub epoch: u64,
    pub order: Vec<String>,
    pub node_count: usize,
    pub internal_count: usize,
    #[serde(flatten)]
    pub envelope: Envelope,
}

// -- expressions ------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct BuildReq {
    pub expr: String,
}

#[derive(Debug, Serialize)]
pub struct NodeResp {
    #[serde(rename = "ref")]
    pub node_ref: NodeRef,
    pub internal_nodes: usize,
    #[serde(flatten)]
    pub envelope: Envelope,
}

// -- apply / restrict -------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct ApplyReq {
    #[serde(rename = "op")]
    pub op: String,
    #[serde(rename = "lhs")]
    pub lhs: NodeRef,
    #[serde(rename = "rhs")]
    pub rhs: Option<NodeRef>,
}

#[derive(Debug, Deserialize)]
pub struct NotReq {
    #[serde(rename = "ref")]
    pub node_ref: NodeRef,
}

#[derive(Debug, Deserialize)]
pub struct RestrictReq {
    #[serde(rename = "ref")]
    pub node_ref: NodeRef,
    pub var: String,
    pub value: bool,
}

#[derive(Debug, Serialize)]
pub struct SatResp {
    pub satisfiable: bool,
    pub witness: Option<BTreeMap<String, bool>>,
    #[serde(flatten)]
    pub envelope: Envelope,
}

// -- garbage collection -----------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct GcReq {
    /// References to preserve; everything unreachable from them is reclaimed.
    #[serde(default)]
    pub roots: Vec<NodeRef>,
}

#[derive(Debug, Serialize)]
pub struct GcResp {
    pub collected: usize,
    pub nodes_before: usize,
    pub nodes_after: usize,
    pub epoch_before: u64,
    pub epoch_after: u64,
    /// Roots repacked at the new epoch (same order as the request).
    pub roots: Vec<NodeRef>,
    #[serde(flatten)]
    pub envelope: Envelope,
}

// -- equivalence ------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct EquivSideReq {
    pub expr: String,
    /// Declared variable order for this side.
    pub order: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct EquivReq {
    pub lhs: EquivSideReq,
    pub rhs: EquivSideReq,
    /// left variable name -> right variable name. If omitted on equal
    /// variable names, the identity mapping is assumed.
    #[serde(default)]
    pub mapping: BTreeMap<String, String>,
    /// Optional per-request override of the exhaustive table limit.
    pub max_assignments: Option<u64>,
    /// Free-form client label: treated as sensitive, only ever redacted.
    #[serde(default)]
    pub client_label: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct EquivResp {
    pub decision: String,
    pub equivalent: bool,
    #[serde(flatten)]
    pub report: FlattenReport,
    #[serde(flatten)]
    pub envelope: Envelope,
}

/// Flattened [`EquivReport`] (serde can't flatten an external type twice in
/// one struct without a helper, so we expose the same fields explicitly).
#[derive(Debug, Serialize)]
pub struct FlattenReport {
    pub structural_equivalent: bool,
    pub oracle_checked: bool,
    pub assignments_checked: u64,
    pub witness: Option<BTreeMap<String, bool>>,
    pub lhs_internal_nodes: usize,
    pub rhs_internal_nodes: usize,
    pub reason: String,
}

impl From<&EquivReport> for FlattenReport {
    fn from(r: &EquivReport) -> Self {
        FlattenReport {
            structural_equivalent: r.structural_equivalent,
            oracle_checked: r.oracle_checked,
            assignments_checked: r.assignments_checked,
            witness: r.witness.clone(),
            lhs_internal_nodes: r.lhs_internal_nodes,
            rhs_internal_nodes: r.rhs_internal_nodes,
            reason: r.reason.clone(),
        }
    }
}

// -- common envelope --------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct Envelope {
    pub diag: crate::diag::Diag,
}

#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub error: ErrorPayload,
    pub diag: crate::diag::Diag,
}

#[derive(Debug, Serialize)]
pub struct ErrorPayload {
    pub kind: String,
    pub message: String,
}
