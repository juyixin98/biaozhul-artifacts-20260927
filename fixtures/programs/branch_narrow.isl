# branch_narrow.isl
# Each branch narrows x to one side of zero; afterwards y is provably >= 1 on
# both paths, so z = y*2 >= 2 and the assertion holds.
let x: [-10, 10];
y := 0;
if x >= 0 {
  y := x + 1;
} else {
  y := 0 - x;
}
z := y * 2;
assert z >= 2;
