# assert_maybe.isl
# x in [0,4]: the assertion holds for 0..2 and fails for 3..4 concretely.
# The static verdict must be maybe_violated (over-approximation uncertainty),
# never a definite failure.
let x: [0, 4];
assert x <= 2;
y := x + 1;
