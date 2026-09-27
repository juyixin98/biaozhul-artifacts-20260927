// Package seqnum implements RFC 793 32-bit sequence-number space arithmetic.
//
// All comparisons between two sequence numbers are performed modulo 2^32:
// a is considered "less than" b when the signed distance (b - a) mod 2^32 is
// in [1, 2^31-1]. SYN and FIN each occupy exactly one sequence number; this
// package only supplies the arithmetic, the flag accounting lives in the
// reassembly package.
package seqnum

// Space is the modulus of the TCP sequence space.
const Space uint64 = 1 << 32

// Half is half the sequence space: the maximum valid window distance. Two
// sequence numbers exactly half a space apart are treated as not ordered
// (see Compare), matching the BSD/macOS convention used by most stacks.
const Half uint32 = 1 << 31

// SEQ is a 32-bit TCP sequence number.
type SEQ uint32

// Add returns s + n modulo 2^32. It panics if n is not within one sequence
// space; callers in this project always add small positive lengths.
func Add(s uint32, n uint64) uint32 {
	if n >= Space {
		panic("seqnum.Add: offset outside one sequence space")
	}
	return s + uint32(n)
}

// Sub returns (a - b) as a signed 32-bit window distance: a value in
// (-2^31, 2^31). Positive means a is "after" b in the current window.
func Sub(a, b uint32) int64 {
	d := int64(uint32(a - b))
	if d >= int64(Half) {
		d -= int64(Space)
	}
	return d
}

// Compare imposes the modular order seen from vantage point v.
//
// Returns -1 if a precedes b, +1 if a follows b, 0 if equal. The tie at the
// half-space boundary resolves to 0 (unordered); reassembly treats an
// unordered segment as outside the receive window rather than guessing.
func Compare(a, b uint32) int {
	switch d := Sub(a, b); {
	case d < 0:
		return -1
	case d > 0:
		return 1
	default:
		return 0
	}
}

// LT reports whether a precedes b in the current window.
func LT(a, b uint32) bool { return Compare(a, b) < 0 }

// LE reports whether a precedes or equals b.
func LE(a, b uint32) bool { return Compare(a, b) <= 0 }

// GT reports whether a follows b.
func GT(a, b uint32) bool { return Compare(a, b) > 0 }

// GE reports whether a follows or equals b.
func GE(a, b uint32) bool { return Compare(a, b) >= 0 }

// BetweenLeft reports a in [left, right) in window order from left.
// Used for the standard TCP test "SND.NXT <= SEG.SEQ < SND.NXT+SND.WND".
func BetweenLeft(seq, left, right uint32) bool {
	return LE(left, seq) && LT(seq, right)
}

// BetweenRight reports a in (left, right] in window order from left.
// Used for the standard TCP test "SND.NXT < SEG.SEQ+LEN <= SND.NXT+SND.WND".
func BetweenRight(end, left, right uint32) bool {
	return LT(left, end) && LE(end, right)
}

// Extend lifts a 32-bit sequence number into a 64-bit absolute value,
// choosing the representative within +/- half a space of the hint (an
// already-extended nearby absolute number, typically rcv.nxt).
func Extend(seq uint32, hint uint64) uint64 {
	base := hint &^ (Space - 1)
	cand := base + uint64(seq)
	// |cand-hint| is at most one sequence space here, so int64 is exact.
	for int64(cand-hint) >= int64(Half) {
		cand -= Space
	}
	for int64(cand-hint) < -int64(Half) {
		cand += Space
	}
	return cand
}
