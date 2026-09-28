//! The three synthetic nets, as typed models and request JSON.

use pn_core::{ArcDef, Net, PlaceDef, Token, TransitionDef};

/// Build place defs from (name, capacity) pairs.
fn places(spec: &[(&str, Token)]) -> Vec<PlaceDef> {
    spec.iter()
        .map(|(n, c)| PlaceDef {
            name: (*n).to_string(),
            capacity: *c,
        })
        .collect()
}

fn arc(place: &str, weight: Token) -> ArcDef {
    ArcDef {
        place: place.to_string(),
        weight,
    }
}

/// A mutex around one unit of a shared resource.
///
/// Places: `idle` (resource free), `busy` (held). acquire consumes `idle` and
/// produces `busy`; release reverses it. P-invariant: `idle + busy = 1`.
pub fn mutex() -> Net {
    Net::new(
        places(&[("idle", 1), ("busy", 1)]),
        vec![
            TransitionDef {
                name: "acquire".into(),
                inputs: vec![arc("idle", 1)],
                outputs: vec![arc("busy", 1)],
            },
            TransitionDef {
                name: "release".into(),
                inputs: vec![arc("busy", 1)],
                outputs: vec![arc("idle", 1)],
            },
        ],
        vec![1, 0],
    )
    .expect("valid mutex net")
}

/// Bounded-buffer producer/consumer.
///
/// Place order is `free`, `ready`, `done`. The `produce` transition moves a
/// token `free -> ready` (filling one buffer slot); `consume` moves `ready ->
/// free` and increments `done`. Capacities are `free = buffer`,
/// `ready = buffer`, `done = steps`. The structural invariant
/// `free + ready = buffer` is exercised by the tests.
pub fn producer_consumer(buffer: Token) -> Net {
    let steps = buffer;
    Net::new(
        places(&[("free", buffer), ("ready", buffer), ("done", steps)]),
        vec![
            TransitionDef {
                name: "produce".into(),
                inputs: vec![arc("free", 1)],
                outputs: vec![arc("ready", 1)],
            },
            TransitionDef {
                name: "consume".into(),
                inputs: vec![arc("ready", 1)],
                outputs: vec![arc("free", 1), arc("done", 1)],
            },
        ],
        vec![buffer, 0, 0],
    )
    .expect("valid producer/consumer net")
}

/// A small net that reaches a deadlock.
///
/// Places are `a`, `b`, `c`. Transition `t1` moves the token `a -> b` and
/// `t2` moves it `b -> c`. Starting with one token on `a`, firing `t1` then
/// `t2` ends at `(0,0,1)` where neither transition is enabled.
pub fn deadlock_net() -> Net {
    Net::new(
        places(&[("a", 1), ("b", 1), ("c", 1)]),
        vec![
            TransitionDef {
                name: "t1".into(),
                inputs: vec![arc("a", 1)],
                outputs: vec![arc("b", 1)],
            },
            TransitionDef {
                name: "t2".into(),
                inputs: vec![arc("b", 1)],
                outputs: vec![arc("c", 1)],
            },
        ],
        vec![1, 0, 0],
    )
    .expect("valid deadlock net")
}

/// Weighted variant: `t1` consumes 2 `a` to make 1 `b`, and `t2` reverses it
/// (1 `b` -> 2 `a`). Exercises weights greater than one and the
/// `a + 2*b`-style invariant. Capacities chosen so the box is tiny.
pub fn weighted_mutex() -> Net {
    Net::new(
        places(&[("a", 2), ("b", 1)]),
        vec![
            TransitionDef {
                name: "pack".into(),
                inputs: vec![arc("a", 2)],
                outputs: vec![arc("b", 1)],
            },
            TransitionDef {
                name: "unpack".into(),
                inputs: vec![arc("b", 1)],
                outputs: vec![arc("a", 2)],
            },
        ],
        vec![2, 0],
    )
    .expect("valid weighted net")
}

/// JSON request for the mutex analysis, including a reachable and an
/// unreachable target.
pub fn mutex_request_json() -> String {
    r#"{
        "schema": "petri-analysis/v1",
        "name": "mutex-fixture",
        "places": [
            {"name": "idle", "capacity": 1},
            {"name": "busy", "capacity": 1}
        ],
        "transitions": [
            {"name": "acquire", "inputs": [{"place": "idle", "weight": 1}],
                                   "outputs": [{"place": "busy", "weight": 1}]},
            {"name": "release", "inputs": [{"place": "busy", "weight": 1}],
                                   "outputs": [{"place": "idle", "weight": 1}]}
        ],
        "initial": [1, 0],
        "targets": [
            {"label": "held", "marking": [0, 1]},
            {"label": "impossible-two-held", "marking": [1, 1]}
        ],
        "options": {"compute_invariants": true, "find_deadlocks": true}
    }"#
    .to_string()
}

/// JSON request for the bounded producer/consumer (buffer size 2).
pub fn producer_consumer_request_json() -> String {
    r#"{
        "schema": "petri-analysis/v1",
        "name": "bounded-buffer-fixture",
        "places": [
            {"name": "free", "capacity": 2},
            {"name": "ready", "capacity": 2},
            {"name": "done", "capacity": 2}
        ],
        "transitions": [
            {"name": "produce", "inputs": [{"place": "free", "weight": 1}],
                                   "outputs": [{"place": "ready", "weight": 1}]},
            {"name": "consume", "inputs": [{"place": "ready", "weight": 1}],
                "outputs": [{"place": "free", "weight": 1}, {"place": "done", "weight": 1}]}
        ],
        "initial": [2, 0, 0],
        "targets": [
            {"label": "one-consumed", "marking": [2, 0, 1]},
            {"label": "token-vanished", "marking": [1, 0, 0]},
            {"label": "buffer-full", "by_place": {"free": 0, "ready": 2, "done": 0}}
        ],
        "options": {"compute_invariants": true, "find_deadlocks": true}
    }"#
    .to_string()
}
