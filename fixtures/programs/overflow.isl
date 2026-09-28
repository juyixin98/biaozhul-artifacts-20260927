# overflow.isl
# Arithmetic follows CHECKED i64 semantics:
#   x*x              fits in i64            -> safe
#   w = x * 4000     fits in i64            -> safe
#   b*b              straddles i64::MAX     -> POSSIBLE overflow (some inputs
#                      overflow, some do not; this is not called a definite bug)
#   big*big          always exceeds i64     -> definite overflow
let x: [1000000, 2000000];
y := x * x;
w := x * 4000;
let b: [3037000499, 3037000501];
p := b * b;
let big: [5000000000, 6000000000];
q := big * big;
# q is a definite overflow: checked i64 execution stops here, so this next
# statement is unreachable (the whole continuation state is empty).
r := 1;
