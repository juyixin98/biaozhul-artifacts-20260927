# unreachable.isl
# x ranges over [0,5], so x > 100 is infeasible: the whole then-branch is
# unreachable. y is exactly 2 at exit.
let x: [0, 5];
if x > 100 {
  y := 1;
} else {
  y := 2;
}
