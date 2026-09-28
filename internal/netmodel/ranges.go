package netmodel

import (
	"math/big"
	"sort"
)

// Range is a half-open interval of address ordinals [Start, End).
// Start <= End is required; an empty range has Start == End.
type Range struct {
	Start *big.Int
	End   *big.Int
}

// String renders [start,end) for logs and traces.
func (r Range) String() string {
	return "[" + r.Start.String() + "," + r.End.String() + ")"
}

// Universe returns [0, 2^width), the full address space for the family.
func Universe(width int) Range {
	return Range{
		Start: big.NewInt(0),
		End:   new(big.Int).Lsh(big.NewInt(1), uint(width)),
	}
}

// PrefixRange converts a real-family Prefix to its ordinal range.
func PrefixRange(p Prefix) Range {
	s, e := p.Interval()
	return Range{Start: s, End: e}
}

// clamp trims r into [0, 2^width). Ranges fully outside are reported empty.
func (r Range) clamp(width int) Range {
	uni := Universe(width)
	s := new(big.Int).Set(r.Start)
	e := new(big.Int).Set(r.End)
	if s.Cmp(uni.Start) < 0 {
		s.Set(uni.Start)
	}
	if e.Cmp(uni.End) > 0 {
		e.Set(uni.End)
	}
	if s.Cmp(e) > 0 {
		return Range{Start: new(big.Int), End: new(big.Int)}
	}
	return Range{Start: s, End: e}
}

// Union merges a collection of ranges into the minimal list of disjoint,
// sorted, adjacent-merged ranges. Touching ranges [0,2) and [2,4) merge into
// [0,4): ordinals are integers, so adjacency carries no gap and is the exact
// precondition for sibling-prefix merges later.
func Union(ranges []Range, width int) []Range {
	normalized := make([]Range, 0, len(ranges))
	for _, r := range ranges {
		r = r.clamp(width)
		if r.Start.Cmp(r.End) < 0 {
			normalized = append(normalized, Range{
				Start: new(big.Int).Set(r.Start),
				End:   new(big.Int).Set(r.End),
			})
		}
	}
	if len(normalized) == 0 {
		return nil
	}
	sort.Slice(normalized, func(i, j int) bool {
		return normalized[i].Start.Cmp(normalized[j].Start) < 0
	})

	merged := make([]Range, 0, len(normalized))
	cur := normalized[0]
	for _, r := range normalized[1:] {
		// r.Start <= cur.End means overlap or exact adjacency (no integer gap).
		if r.Start.Cmp(cur.End) <= 0 {
			if r.End.Cmp(cur.End) > 0 {
				cur.End.Set(r.End)
			}
			continue
		}
		merged = append(merged, cur)
		cur = r
	}
	merged = append(merged, cur)
	return merged
}

// Subtract returns the set difference a \\ b: every ordinal in a that is not in
// b. Both inputs are first normalised via Union, so the result is again
// sorted, disjoint and adjacent-merged.
func Subtract(a, b []Range, width int) []Range {
	A := Union(a, width)
	B := Union(b, width)
	if len(A) == 0 {
		return nil
	}
	if len(B) == 0 {
		return A
	}

	var out []Range
	for _, ra := range A {
		cursor := new(big.Int).Set(ra.Start)
		for _, rb := range B {
			// Skip b-ranges wholly before the unconsumed tail.
			if rb.End.Cmp(cursor) <= 0 {
				continue
			}
			// B is sorted; once rb starts past ra's end there is nothing left.
			if rb.Start.Cmp(ra.End) >= 0 {
				break
			}
			// Gap before rb becomes output.
			if rb.Start.Cmp(cursor) > 0 {
				out = append(out, Range{
					Start: new(big.Int).Set(cursor),
					End:   new(big.Int).Set(rb.Start),
				})
			}
			if rb.End.Cmp(cursor) > 0 {
				cursor.Set(rb.End)
			}
			if cursor.Cmp(ra.End) >= 0 {
				break
			}
		}
		if cursor.Cmp(ra.End) < 0 {
			out = append(out, Range{
				Start: new(big.Int).Set(cursor),
				End:   new(big.Int).Set(ra.End),
			})
		}
	}
	return Union(out, width)
}

// Size returns the number of ordinals covered by a set of normalised ranges.
func Size(ranges []Range) *big.Int {
	total := new(big.Int)
	for _, r := range ranges {
		total.Add(total, new(big.Int).Sub(r.End, r.Start))
	}
	return total
}
