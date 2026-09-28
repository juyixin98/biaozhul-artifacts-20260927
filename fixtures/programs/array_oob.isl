# array_oob.isl
# a[i] with i in [0,20] vs len 10: part of the interval is in bounds, part is
# not -> POSSIBLE out-of-bounds (over-approximation, not a proven failure).
# a[j] with j = -5: wholly outside [0,9] -> definite out-of-bounds.
let i: [0, 20];
array a[10];
x := a[i];
j := 0 - 5;
y := a[j];
