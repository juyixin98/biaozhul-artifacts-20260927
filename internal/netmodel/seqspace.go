package netmodel

// Window size (half of the 32-bit ring). A segment whose forward distance from
// a reference sequence number is below this value is "ahead"; above it is
// "behind". This is the standard PAWS-free modular comparison from
// RFC 9293 §3.2.1 (sequence numbers arrive from a 2^32 circular space).
const WindowHalf = int64(1) << 31

// FwdDist returns the signed forward distance from ref to seq in the circular
// space: values in (-2^31, 2^31]. A positive result means seq comes after ref;
// a negative result means seq (or its data) is before ref / already seen.
func FwdDist(seq, ref uint32) int64 {
	return int64(int32(seq - ref))
}

// SeqBefore reports whether s is strictly before t in circular order.
func SeqBefore(s, t uint32) bool { return FwdDist(s, t) < 0 }

// SeqAfter reports whether s is strictly after t in circular order.
func SeqAfter(s, t uint32) bool { return FwdDist(s, t) > 0 }

// SeqAdd adds n (may be negative, may exceed 32 bits) to a raw sequence number.
func SeqAdd(seq uint32, n int64) uint32 { return uint32(int64(seq) + n) }

// seqAnchor pins one raw 32-bit sequence number to one monotonic coordinate.
type seqAnchor struct {
	raw uint32
	abs int64
}

// SeqMapper projects raw 32-bit TCP sequence numbers into an unwrapped
// int64 coordinate system. It records anchors for every unambiguous sequence
// position that has been observed (SYN ISN, segment starts, FIN position) and
// projects a query against the nearest anchor in circular distance; that
// ambiguity window (~2 GiB in each direction) is the inherent limitation of
// comparing isolated 32-bit values, not a coding shortcut.
//
// Coordinate convention for a direction:
//   - with a SYN observed: the SYN itself occupies coordinate 0 and the first
//     data byte is at coordinate 1 (SYN consumes one sequence number);
//   - without a SYN observed ("missing handshake"): the first observed data
//     byte is anchored at coordinate 0 and offsets are relative to it; the
//     true ISN-relative offset is reported as undecidable.
type SeqMapper struct {
	anchors []seqAnchor
}

// Reset removes all anchors.
func (m *SeqMapper) Reset() { m.anchors = m.anchors[:0] }

// AddAnchor pins raw at absolute coordinate abs. A new anchor is kept unless it
// is redundant with an existing one (it maps to the same coordinate).
func (m *SeqMapper) AddAnchor(raw uint32, abs int64) {
	for _, a := range m.anchors {
		if a.raw == raw {
			return
		}
	}
	m.anchors = append(m.anchors, seqAnchor{raw: raw, abs: abs})
}

// Empty reports whether any anchor exists.
func (m *SeqMapper) Empty() bool { return len(m.anchors) == 0 }

// Abs projects a raw sequence number to a monotonic coordinate. It must not be
// called on an empty mapper.
func (m *SeqMapper) Abs(raw uint32) int64 {
	best := m.anchors[0]
	bestD := abs64(FwdDist(raw, best.raw))
	for _, a := range m.anchors[1:] {
		if d := abs64(FwdDist(raw, a.raw)); d < bestD {
			best, bestD = a, d
		}
	}
	return best.abs + FwdDist(raw, best.raw)
}

// AbsWith returns the monotonic coordinate, registering the given anchor first
// when none exists. The bool is false if the mapper was empty (callers can use
// this to label the result as relative-to-first-observation).
func (m *SeqMapper) AbsWith(raw uint32) (int64, bool) {
	if m.Empty() {
		m.AddAnchor(raw, 0)
		return 0, false
	}
	return m.Abs(raw), true
}

// LastAnchor returns the raw/abs pair of the anchor with the greatest absolute
// coordinate and true, or zero values/false when empty.
func (m *SeqMapper) LastAnchor() (uint32, int64, bool) {
	if m.Empty() {
		return 0, 0, false
	}
	best := m.anchors[0]
	for _, a := range m.anchors[1:] {
		if a.abs > best.abs {
			best = a
		}
	}
	return best.raw, best.abs, true
}

// Export returns the anchors as parallel slices for persistence.
func (m *SeqMapper) Export() (raw []uint32, abs []int64) {
	for _, a := range m.anchors {
		raw = append(raw, a.raw)
		abs = append(abs, a.abs)
	}
	return
}

// Load replaces all anchors from persisted parallel slices.
func (m *SeqMapper) Load(raw []uint32, abs []int64) {
	m.anchors = m.anchors[:0]
	for i := range raw {
		m.anchors = append(m.anchors, seqAnchor{raw: raw[i], abs: abs[i]})
	}
}

func abs64(x int64) int64 {
	if x < 0 {
		return -x
	}
	return x
}
