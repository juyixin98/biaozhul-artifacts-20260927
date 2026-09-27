# Bounded-integer semantics

This document is the normative reference the parser, the concrete executor
and the abstract interpreter all implement. Where the three could disagree,
the exhaustive differential test suite (`ia-tests`) is the arbiter.

## 1. Values

Every value is a **bounded signed 64-bit integer**, exactly Rust's `i64`
(range `[-9_223_372_036_854_775_808, 9_223_372_036_854_775_807]`).

* There is **no** mathematical/unbounded integer type anywhere in the
  language or the domain.
* The abstract domain has no separate `-∞/+∞` objects: when widening needs an
  unbounded endpoint it uses the bounded sentinels `i64::MIN` / `i64::MAX`,
  meaning "the whole representable domain".
* Integer literals outside the range are rejected at lex time. The single
  token `9223372036854775808` (= 2^63) is accepted only directly after a
  unary `-`, so that `-9223372036854775808` denotes `i64::MIN`.

## 2. Arithmetic and failure

Arithmetic uses **checked** i64 semantics (Rust `checked_add`,
`checked_div`, …). Any of the following is a *runtime failure*:

| operation | failure |
|---|---|
| `+`, `-`, `*` | result outside the i64 range |
| unary `-` | operand is `i64::MIN` |
| `/`, `%` | divisor `0` |
| `/` | `i64::MIN / -1` (result would be 2^63) |
| `a[i]` read or write | `i < 0` or `i >= length(a)` |
| `assert(e)` | `e == 0` |

There is **no wrapping**. A failed operation does not produce a value.

Division truncates toward zero and the remainder satisfies
`x == (x/y)*y + x%y`, i.e. Rust/C semantics:

```text
-7 / 3 == -2     -7 % 3 == -1
 7 / 3 ==  2      7 % 3 ==  1
```

Comparisons and logical operators never fail and yield exactly `0` (false)
or `1` (true). `&&` / `||` are **non-short-circuiting**: both operands are
evaluated, so a fault hidden behind a short-circuited operand is still
observable and still checked.

## 3. Declarations, inputs, initialisation

* `input x [lo:hi];` — x is chosen non-deterministically from the closed
  interval `[lo, hi]`. Concrete verification enumerates the Cartesian product
  of all input ranges.
* `const c = k;` — immutable i64 constant.
* `array a[n];` — fixed length, zero-initialised; `1 <= n <= 2^20`.
* Any other scalar identifier is an **implicit local**, zero-initialised
  before the program body runs (matching zero-initialised arrays). It can be
  read on any path; its concrete value is `0` until assigned. This keeps the
  language total (no "uninitialised variable" failure category).

## 4. Control flow and non-termination

* `if (c) t else e` — concrete execution takes exactly one branch.
* `while (c) b` — concrete execution loops until `c == 0`. A per-execution
  **step limit** (`step_limit`, default 200 000) converts a non-terminating
  run into the distinct `step_limit` failure category instead of hanging.
* The abstract interpreter always terminates by construction (see
  [ARCHITECTURE.md](ARCHITECTURE.md) §loop fixpoints); it reports results
  even for programs whose concrete executions diverge.

## 5. Verdict vocabulary

Every checkable site is given exactly one verdict:

* **safe** — no concrete execution reaching the site can fail;
* **possible_failure** — the abstract state admits both failing and
  non-failing choices (or the domain cannot split them). This is an
  **over-approximation**, explicitly *not* a claim that the program is
  wrong;
* **guaranteed_failure** — every concrete execution reaching the site fails;
* **unreachable** — no concrete execution reaches the site under the declared
  input ranges, so no runtime failure is possible there.

"Possible" is never phrased as a definite error in reports.

## 6. Soundness contract

For every program and every site:

1. a concrete failure can only be reported at a site the analysis labels
   `possible_failure` or `guaranteed_failure`, with the same failure category
   and source offset;
2. a concrete scalar exit value always lies inside the abstract exit interval;
3. a concretely reached site is never labelled `unreachable`;
4. a `guaranteed_failure` verdict over a fully enumerated domain is witnessed
   by at least one concrete failing execution.

`ia-verify` checks 1–4 by enumerating inputs against the independent concrete
executor. If enumeration exceeds the configured cap, verification is reported
**not run** — it is never silently marked passed.
