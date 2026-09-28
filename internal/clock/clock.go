// Package clock implements the pure vector-clock arithmetic used by the
// causal layer. It has no I/O and no dependency on the broadcast core, so both
// the core and the independent test oracle can use it while keeping their
// deliverability decisions implemented separately.
package clock

import "cbcast/internal/protocol"

// New returns a zeroed clock over the given membership.
func New(members []string) protocol.VC {
	c := make(protocol.VC, len(members))
	for _, m := range members {
		c[m] = 0
	}
	return c
}

// Clone returns a copy.
func Clone(c protocol.VC) protocol.VC {
	out := make(protocol.VC, len(c))
	for k, v := range c {
		out[k] = v
	}
	return out
}

// Tick increments the local component (called exactly once per local event).
func Tick(c protocol.VC, node string) protocol.VC {
	c[node]++
	return c
}

// Merge sets each component to the component-wise max.
func Merge(dst protocol.VC, src protocol.VC) protocol.VC {
	for k, v := range src {
		if v > dst[k] {
			dst[k] = v
		}
	}
	return dst
}

// LE reports whether a <= b component-wise (a happens-before-or-equal b).
// Components present in one clock but absent from the other are treated as 0.
func LE(a, b protocol.VC) bool {
	seen := make(map[string]struct{}, len(a)+len(b))
	for k := range a {
		seen[k] = struct{}{}
		if a[k] > b[k] {
			return false
		}
	}
	for k := range b {
		if _, ok := seen[k]; ok {
			continue
		}
		if 0 > b[k] { // unsigned zero; always false; kept for clarity
			return false
		}
	}
	return true
}

// HB reports whether a strictly happens-before b: a <= b and a != b.
func HB(a, b protocol.VC) bool {
	if !LE(a, b) {
		return false
	}
	// strict: at least one component of b exceeds a.
	for k, v := range b {
		if v > a[k] {
			return true
		}
	}
	return false
}

// Concurrent reports a || b (neither happens-before the other, clocks differ).
func Concurrent(a, b protocol.VC) bool {
	return !LE(a, b) && !LE(b, a)
}

// Equal compares clocks component-wise (missing components treated as 0).
func Equal(a, b protocol.VC) bool {
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	for k, v := range b {
		if a[k] != v {
			return false
		}
	}
	return true
}

// MissingGaps returns, for an event clock c, every (node, seq) predecessor that
// has not yet been delivered according to deliveredClock.
//
// Event e with clock V is deliverable exactly when the receiver's delivered
// clock D satisfies D[node] >= V[node] - 1 for node == sender and
// D[node] >= V[node] for all other nodes. Gaps are expressed as 1-based
// per-sender sequence numbers.
func MissingGaps(c protocol.VC, sender string, deliveredClock protocol.VC) []protocol.Gap {
	var gaps []protocol.Gap
	for node, v := range c {
		want := v
		if node == sender {
			want = v - 1
		}
		have := deliveredClock[node]
		if have < want {
			for seq := have + 1; seq <= want; seq++ {
				gaps = append(gaps, protocol.Gap{Sender: node, Seq: seq})
			}
		}
	}
	return gaps
}

// Deliverable reports whether clock c (authored by sender) may be delivered on
// top of deliveredClock.
func Deliverable(c protocol.VC, sender string, deliveredClock protocol.VC) bool {
	return len(MissingGaps(c, sender, deliveredClock)) == 0
}
