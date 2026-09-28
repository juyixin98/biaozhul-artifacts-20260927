//! Success envelopes and conversion of kernel diagnostic traces into JSON.
//!
//! The kernel crate has no serde dependency, so traces are mapped here with
//! small explicit mappers. Every answer carries the request id, index
//! location/version and the navigation steps that produced it.

use serde_json::{json, Value};
use wm_core::wavelet::{
    CountNavStep, CountTrace, NeighborSide, NeighborTrace, QuantileStep, QuantileTrace, RankLtTrace,
};

/// Build the standard success envelope.
pub fn ok(request_id: &str, data: Value, diagnostics: Option<Value>) -> Value {
    let mut v = json!({
        "ok": true,
        "request_id": request_id,
        "data": data,
    });
    if let Some(diag) = diagnostics {
        v["diagnostics"] = diag;
    }
    v
}

/// Static context about where/how an answer was produced.
pub fn index_context(
    name: &str,
    location: &str,
    format_version: u32,
    bit_len: usize,
    levels: usize,
) -> Value {
    json!({
        "index": name,
        "location": location,
        "format_version": format_version,
        "bit_len": bit_len,
        "levels": levels,
        "semantics": {
            "range": "half-open [l, r)",
            "k": "zero-based order statistic index",
            "value_range": "half-open [lo, hi)"
        }
    })
}

fn quantile_step_json(s: &QuantileStep) -> Value {
    json!({
        "level": s.depth,
        "inspected_bit_position": s.bit_position,
        "range_before": [s.range_before.0, s.range_before.1],
        "zeros_in_range": s.zeros_in_range,
        "k_before": s.k_before,
        "chosen_branch": if s.chosen_bit == 0 { "zero(left)" } else { "one(right)" },
        "range_after": [s.range_after.0, s.range_after.1],
    })
}

/// Convert a k-th-smallest trace.
pub fn quantile_trace_json(t: &QuantileTrace) -> Value {
    json!({
        "query": {"l": t.query_l, "r": t.query_r, "k": t.k},
        "compressed_id": t.id,
        "steps": t.steps.iter().map(quantile_step_json).collect::<Vec<_>>(),
        "result_value": t.value,
    })
}

fn count_step_json(s: &CountNavStep) -> Value {
    json!({
        "level": s.depth,
        "inspected_bit_position": s.bit_position,
        "range_before": [s.range_before.0, s.range_before.1],
        "bound_bit": s.bound_bit,
        "matched_zeros_here": s.matched_zeros_here,
        "range_after": [s.range_after.0, s.range_after.1],
    })
}

fn rank_lt_json(t: &RankLtTrace, label: &str) -> Value {
    let mut v = json!({
        "label": label,
        "target": t.target,
        "distinct_values_below_target": t.bound,
        "count": t.count,
        "steps": t.steps.iter().map(count_step_json).collect::<Vec<_>>(),
    });
    if let Some(note) = t.note {
        v["note"] = json!(note);
    }
    v
}

/// Convert a value-range count trace; the subtraction is shown explicitly.
pub fn count_trace_json(t: &CountTrace) -> Value {
    json!({
        "value_range": {"lo": t.lo, "hi": t.hi, "shape": "half-open"},
        "below_hi": rank_lt_json(&t.below_hi, "count(< hi)"),
        "below_lo": match &t.below_lo {
            Some(b) => rank_lt_json(b, "count(< lo)"),
            None => Value::Null,
        },
        "subtraction": {
            "formula": "count([lo,hi)) = count(< hi) - count(< lo)",
            "count_below_hi": t.below_hi.count,
            "count_below_lo": t.below_lo.as_ref().map(|b| b.count),
        },
        "count": t.count,
        "note": t.note,
    })
}

/// Convert a predecessor/successor trace. Equality and "no neighbor" are
/// reported as separate fields rather than folded into the value.
pub fn neighbor_trace_json(t: &NeighborTrace) -> Value {
    json!({
        "kind": match t.side {
            NeighborSide::Predecessor => "predecessor",
            NeighborSide::Successor => "successor",
        },
        "x": t.x,
        "count_less_than_x": t.count_lt,
        "count_less_or_equal_x": t.count_le,
        "x_present_in_range": t.present,
        "neighbor_exists": t.value.is_some(),
        "value": t.value,
        "selection_trace": t.selection.as_ref().map(quantile_trace_json),
        "uncertain": [],
    })
}
