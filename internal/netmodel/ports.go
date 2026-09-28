package netmodel

import (
	"fmt"
	"strconv"
	"strings"
)

// PortMax is the inclusive upper bound of a port interval.
const PortMax = 65535

// PortInterval is a closed inclusive port range [Lo, Hi].
type PortInterval struct {
	Lo uint16
	Hi uint16
}

// FullPorts is 0-65535.
var FullPorts = PortInterval{Lo: 0, Hi: PortMax}

// ParsePortInterval accepts "80", "8000-9000", "any"/"*" (0-65535).
func ParsePortInterval(s string) (PortInterval, error) {
	q := strings.TrimSpace(strings.ToLower(s))
	if q == "any" || q == "*" || q == "" {
		return FullPorts, nil
	}
	if !strings.Contains(q, "-") {
		n, err := parsePort(q)
		if err != nil {
			return PortInterval{}, err
		}
		return PortInterval{Lo: n, Hi: n}, nil
	}
	parts := strings.SplitN(q, "-", 2)
	lo, err := parsePort(strings.TrimSpace(parts[0]))
	if err != nil {
		return PortInterval{}, err
	}
	hi, err := parsePort(strings.TrimSpace(parts[1]))
	if err != nil {
		return PortInterval{}, err
	}
	if lo > hi {
		return PortInterval{}, fmt.Errorf("invalid port interval %q: low > high", s)
	}
	return PortInterval{Lo: lo, Hi: hi}, nil
}

func parsePort(s string) (uint16, error) {
	n, err := strconv.ParseUint(s, 10, 16)
	if err != nil {
		return 0, fmt.Errorf("invalid port %q", s)
	}
	return uint16(n), nil
}

func (p PortInterval) String() string {
	if p.Lo == 0 && p.Hi == PortMax {
		return "any"
	}
	if p.Lo == p.Hi {
		return strconv.Itoa(int(p.Lo))
	}
	return fmt.Sprintf("%d-%d", p.Lo, p.Hi)
}

// Contains reports whether v falls in the interval.
func (p PortInterval) Contains(v uint16) bool { return v >= p.Lo && v <= p.Hi }

// Count returns the number of ports in the interval.
func (p PortInterval) Count() uint64 { return uint64(p.Hi) - uint64(p.Lo) + 1 }

func intervalsOverlap(a, b PortInterval) bool {
	return a.Lo <= b.Hi && b.Lo <= a.Hi
}

// PartitionIntervals returns the minimal set of disjoint closed intervals
// covering exactly the union of the inputs (e.g. [80,100] and [90,120] split
// into [80,89],[90,100],[101,120]). An empty input returns nil.
func PartitionIntervals(in []PortInterval) []PortInterval {
	if len(in) == 0 {
		return nil
	}
	bounds := map[int]struct{}{}
	for _, iv := range in {
		bounds[int(iv.Lo)] = struct{}{}
		bounds[int(iv.Hi)+1] = struct{}{}
	}
	pts := make([]int, 0, len(bounds))
	for b := range bounds {
		pts = append(pts, b)
	}
	// simple sort
	for i := 1; i < len(pts); i++ {
		for j := i; j > 0 && pts[j-1] > pts[j]; j-- {
			pts[j-1], pts[j] = pts[j], pts[j-1]
		}
	}
	covered := func(v int) bool {
		for _, iv := range in {
			if int(iv.Lo) <= v && v <= int(iv.Hi) {
				return true
			}
		}
		return false
	}
	// IMPORTANT: adjacent covered segments are NOT merged. Each segment is
	// bounded by some rule's endpoints (or endpoint+1), so it is fully inside
	// or fully outside every input interval; merging would destroy that
	// atomicity (e.g. [80,90] vs [91,100] must stay split at 91).
	var out []PortInterval
	for i := 0; i+1 < len(pts); i++ {
		lo := pts[i]
		hi := pts[i+1] - 1
		if lo > hi || !covered(lo) {
			continue
		}
		out = append(out, PortInterval{Lo: uint16(lo), Hi: uint16(hi)})
	}
	return out
}
