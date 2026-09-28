# loop_grow.isl
# i grows monotonically from 0 up to n; array a is indexed by i.
# Widening is needed to stabilise the loop head; narrowing sharpens the scalar
# head [0, inf] back to [0, 10]. (The weak-updated array element summary keeps
# [0, inf]: each iteration's stored values depend on the pre-narrowed head,
# which is a documented sound imprecision of weak updates, not an error.)
let n: [0, 10];
array a[10];
i := 0;
while i < n {
  a[i] := i * 2;
  i := i + 1;
}
