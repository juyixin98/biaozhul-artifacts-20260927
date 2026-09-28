package reassembly

import (
	"sort"
)

// ByteRun is one contiguous accepted interval [Start, End) of a direction.
type ByteRun struct {
	Start int64  `json:"start"`
	End   int64  `json:"end"`
	Data  []byte `json:"-"`
}

// Gap is a missing interval proved by evidence: bytes at [Start,End) were
// never delivered while later bytes are present (or FIN proves the end).
type Gap struct {
	Start int64 `json:"start"`
	End   int64 `json:"end"` // -1 means "to end unknown" when no FIN exists
}

// PutResult reports what a segment insertion did, byte by byte in aggregate.
type PutResult struct {
	NewBytes       int64 // positions filled that were previously absent
	IdenticalBytes int64 // retransmitted bytes matching established data
	ConflictBytes  int64 // positions where the offered byte disagreed
	Start, End     int64 // coordinate span covered by the offered segment
}

// sparseBuffer stores one direction's accepted payload as a coordinate ->
// byte map plus the owning record id per coordinate. Coordinates are the
// unwrapped monotonic values produced by netmodel.SeqMapper, so wrap handling
// is entirely upstream; this type never sees raw uint32 sequence numbers.
//
// The map representation is deliberately simple and auditable; it targets
// captures up to a few tens of MB per direction (see docs/ARCHITECTURE.md for
// scaling notes).
type sparseBuffer struct {
	cells  map[int64]byte
	owners map[int64]string
}

func newSparseBuffer() *sparseBuffer {
	return &sparseBuffer{cells: map[int64]byte{}, owners: map[int64]string{}}
}

// put inserts data whose first byte is at coordinate start, under the given
// policy. Every disagreement becomes an entry in conflicts; identical bytes
// are counted separately so retransmissions can never duplicate output.
func (b *sparseBuffer) put(start int64, data []byte, recordID string, policy OverlapPolicy, rawSeqFor func(int64) uint32) (PutResult, []byteConflict) {
	var res PutResult
	res.Start, res.End = start, start+int64(len(data))
	var conflicts []byteConflict

	for i, v := range data {
		pos := start + int64(i)
		existing, occupied := b.cells[pos]
		switch {
		case !occupied:
			b.cells[pos] = v
			b.owners[pos] = recordID
			res.NewBytes++
		case existing == v:
			res.IdenticalBytes++
		default:
			res.ConflictBytes++
			c := byteConflict{
				offset:        pos,
				rawSeq:        rawSeqFor(pos),
				existing:      existing,
				offered:       v,
				existingOwner: b.owners[pos],
			}
			switch policy {
			case PolicyLastWins:
				b.cells[pos] = v
				b.owners[pos] = recordID
				c.disposition = DispReplaced
			case PolicyQuarantine:
				c.disposition = DispQuarantined
			default: // PolicyFirstWins
				c.disposition = DispRejected
			}
			conflicts = append(conflicts, c)
		}
	}
	return res, conflicts
}

// contiguous returns the accepted prefix [lo, hi): every position filled.
func (b *sparseBuffer) contiguous(lo int64) (int64, bool) {
	if _, ok := b.cells[lo]; !ok {
		return lo, false
	}
	hi := lo + 1
	for {
		if _, ok := b.cells[hi]; !ok {
			return hi, true
		}
		hi++
	}
}

// presentSorted returns all occupied coordinates in ascending order.
func (b *sparseBuffer) presentSorted() []int64 {
	out := make([]int64, 0, len(b.cells))
	for off := range b.cells {
		out = append(out, off)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// runs returns maximal intervals of accepted bytes with their contents.
//
// confirmedBelow limits which runs count as "continuously evidenced": callers
// pass the first gap position so runs at/after it are reported as held
// out-of-order evidence rather than as stream output.
func (b *sparseBuffer) runs(confirmedBelow int64) []ByteRun {
	off := b.presentSorted()
	if len(off) == 0 {
		return nil
	}
	var runs []ByteRun
	start, prev := off[0], off[0]
	flush := func(end int64, confirmed bool) {
		if !confirmed {
			return
		}
		data := make([]byte, 0, end-start)
		for p := start; p < end; p++ {
			data = append(data, b.cells[p])
		}
		runs = append(runs, ByteRun{Start: start, End: end, Data: data})
	}
	for _, o := range off[1:] {
		if o == prev+1 {
			prev = o
			continue
		}
		flush(prev+1, start < confirmedBelow)
		start, prev = o, o
	}
	flush(prev+1, start < confirmedBelow)
	return runs
}

// heldRuns returns accepted-but-not-yet-contiguous intervals (data sitting
// beyond a gap). It never contributes to the replayed byte stream.
func (b *sparseBuffer) heldRuns(confirmedBelow int64) []ByteRun {
	off := b.presentSorted()
	if len(off) == 0 {
		return nil
	}
	var runs []ByteRun
	start, prev := off[0], off[0]
	flush := func(end int64) {
		if start < confirmedBelow {
			return // contiguous or part of confirmed prefix
		}
		data := make([]byte, 0, end-start)
		for p := start; p < end; p++ {
			data = append(data, b.cells[p])
		}
		runs = append(runs, ByteRun{Start: start, End: end, Data: data})
	}
	for _, o := range off[1:] {
		if o == prev+1 {
			prev = o
			continue
		}
		flush(prev + 1)
		start, prev = o, o
	}
	flush(prev + 1)
	return runs
}

// gaps returns proved holes within the observed extent. A position is a gap
// only when evidence brackets it: an accepted byte must exist later in the
// stream. When finSeen is true the FIN position proves the end, so the hole
// immediately before it counts; otherwise an open tail with nothing after it
// is merely "not yet received" and is never reported (no fabricated bounds).
func (b *sparseBuffer) gaps(base, hi int64, finSeen bool) []Gap {
	var out []Gap
	pos := base
	for pos < hi {
		if _, ok := b.cells[pos]; ok {
			pos++
			continue
		}
		start := pos
		for pos < hi {
			if _, ok := b.cells[pos]; ok {
				break
			}
			pos++
		}
		switch {
		case pos < hi:
			// Accepted bytes follow the hole -> gap is proved.
			out = append(out, Gap{Start: start, End: pos})
		case finSeen:
			// FIN proves the stream ends here -> terminal hole is proved.
			out = append(out, Gap{Start: start, End: pos})
		default:
			// No later evidence: open tail, not a reportable gap.
		}
	}
	return out
}
