//! Fixture specifications in textual DSL, plus the property strings each
//! fixture is checked against.

/// Two-process mutex protocol, **correct** version.
///
/// `f1/f2` record whether each process wants to enter; `c` is the shared
/// lock. A process must take the lock (`try_enter`) before entering, and
/// leaves the lock on exit. Mutual exclusion `!(in1 && in2)` is an invariant.
///
/// Here "in" is represented by `f == in && locked == who`, but to keep the
/// update language free of booleans-on-integers we track two explicit
/// inside-flags `in1`, `in2` set atomically with the lock acquisition.
pub fn mutex_safe() -> &'static str {
    r#"
system mutex_safe {
  var {
    in1: bool;
    in2: bool;
    locked: bool
  }
  init { in1 := false, in2 := false, locked := false }
  transition t1_enter {
    guard: !in1 && !locked;
    then: in1 := true, locked := true
  }
  transition t2_enter {
    guard: !in2 && !locked;
    then: in2 := true, locked := true
  }
  transition t1_leave {
    guard: in1;
    then: in1 := false, locked := false
  }
  transition t2_leave {
    guard: in2;
    then: in2 := false, locked := false
  }
}
"#
}

/// Two-process mutex protocol, **flawed** version: `t1_enter` does not test
/// the lock, so process 1 can barrel in while process 2 is inside.
pub fn mutex_bad() -> &'static str {
    r#"
system mutex_bad {
  var {
    in1: bool;
    in2: bool;
    locked: bool
  }
  init { in1 := false, in2 := false, locked := false }
  transition t1_enter {
    guard: !in1;                 // BUG: no `&& !locked`
    then: in1 := true, locked := true
  }
  transition t2_enter {
    guard: !in2 && !locked;
    then: in2 := true, locked := true
  }
  transition t1_leave {
    guard: in1;
    then: in1 := false, locked := false
  }
  transition t2_leave {
    guard: in2;
    then: in2 := false, locked := false
  }
}
"#
}

/// Mutual-exclusion safety property shared by both mutex fixtures.
pub const MUTEX_INVARIANT: &str = "!(in1 && in2)";

/// Bounded counter: `x` walks 0..3, increments stop at the cap, and `x == 3`
/// is declared a legal terminal. No deadlock exists.
pub fn counter() -> &'static str {
    r#"
system counter {
  var { x: int[0..3] }
  init { x := 0 }
  transition inc {
    guard: x < 3;
    then: x := x + 1
  }
  transition reset {
    guard: x > 0;
    then: x := 0
  }
  terminal { x == 3 }
}
"#
}

/// Counter variant with **no** terminal clause: state 3 then has zero enabled
/// transitions and is a genuine deadlock.
pub fn counter_deadlock() -> &'static str {
    r#"
system counter_deadlock {
  var { x: int[0..3] }
  init { x := 0 }
  transition inc {
    guard: x < 3;
    then: x := x + 1
  }
}
"#
}

/// A system whose init predicate is unsatisfiable over its domain.
pub fn no_init() -> &'static str {
    r#"
system no_init {
  var { x: int[0..2] }
  init { x >= 10 }
  transition inc {
    guard: x < 2;
    then: x := x + 1
  }
}
"#
}

/// Large state space for budget-truncation tests: 5001 states far beyond the
/// fixture-level budgets used in tests.
pub fn big_counter() -> &'static str {
    r#"
system big_counter {
  var { x: int[0..5000] }
  init { x := 0 }
  transition inc {
    guard: x < 5000;
    then: x := x + 1
  }
  transition dec {
    guard: x > 0;
    then: x := x - 1
  }
}
"#
}

/// Parallel-assignment semantics fixture: `x := y, y := x` must swap using
/// the same pre-state. After `swap` from the initial state the pair is
/// (2, 1), never (1, 1) or (2, 2).
pub fn swap() -> &'static str {
    r#"
system swap {
  var { x: int[0..3]; y: int[0..3] }
  init { x := 1, y := 2 }
  transition sw {
    guard: true;
    then: x := y, y := x
  }
}
"#
}

/// JSON-format smoke fixture, mirroring `counter` but expressed as JSON.
pub fn counter_json() -> serde_json::Value {
    serde_json::json!({
        "name": "counter_json",
        "variables": [
            {"name": "x", "type": "int", "lo": 0, "hi": 3}
        ],
        "init": {"state": {"x": 0}},
        "terminal": "x == 3",
        "transitions": [
            {"name": "inc", "guard": "x < 3", "assign": [{"target": "x", "expr": "x + 1"}]},
            {"name": "reset", "guard": "x > 0", "assign": [{"target": "x", "value": 0}]}
        ]
    })
}

pub const COUNTER_INVARIANT: &str = "x >= 0 && x <= 3";
pub const COUNTER_UNREACHABLE_ERROR: &str = "x == 5";
pub const COUNTER_REACHES_CAP: &str = "x == 3";
pub const SWAP_POSTCONDTION_EF: &str = "x == 2 && y == 1";
