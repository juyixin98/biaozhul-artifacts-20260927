package reassembly

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"sort"

	"tcpreasm/internal/config"
	"tcpreasm/internal/diag"
)

// block is one contiguous run of buffered bytes with a single contributing
// record. Overlap resolution splits blocks at arbitrary boundaries; blocks
// abutting from different records are kept separate so per-byte provenance
// is never ambiguous.
type block struct {
	start    uint64
	data     []byte
	recordID string
}

func (b *block) end() uint64 { return b.start + uint64(len(b.data)) }

// slice returns the bytes covering [s,e) (which must intersect the block).
func (b *block) slice(s, e uint64) []byte {
	lo := s - b.start
	hi := e - b.start
	return b.data[lo:hi]
}

// conflictEvent is one contradictory range found during insertion.
type conflictEvent struct {
	startAbs, endAbs uint64
	incumbentRec     string
	newcomerRec      string
	incumbentSHA     string
	newcomerSHA      string
	incumbentBytes   []byte
	newcomerBytes    []byte
	// delivered is true when the incumbent bytes were already emitted.
	delivered bool
}

// dirAssembler performs single-direction reassembly in absolute 64-bit
// sequence space (wrap lifting happens before bytes arrive here).
type dirAssembler struct {
	flowKey   string
	genIndex  int
	direction string
	policy    config.OverlapPolicy
	previewN  int
	maxBuffer int
	evTail    int

	inferred bool
	hasISN   bool
	isn      uint32 // original 32-bit ISN (SYN seq)

	// rcvNxt is the next undelivered absolute sequence. Set on SYN
	// (ISN+1) or inferred from the first data-bearing segment.
	rcvNxt uint64
	// synAbs is the absolute seq of the SYN; stream offset zero starts at
	// synAbs+1.
	synAbs uint64

	// blocks holds unpoisoned buffered data: sorted, non-overlapping.
	blocks []*block
	// poisoned blocks cover quarantined bytes; they block delivery while
	// remaining in evidence.
	poisoned []*block

	// evidence ring: the tail of delivered bytes keyed by absolute seq.
	evStart uint64
	evData  []byte
	evSet   bool // true once the first delivered byte establishes evStart

	buffered int

	// curGap is the single hole interval (stream offsets) currently ahead
	// of the delivered prefix, used to avoid duplicate gap evidence.
	curGap *[2]uint64

	finSeen   bool
	finAbs    uint64 // absolute seq consumed by the FIN
	finRecord string
	finDone   bool // FIN closing sequence reached and confirmed

	reset bool
}

// off maps an absolute data sequence number to a stream offset (byte after
// the ISN).
func (d *dirAssembler) off(abs uint64) uint64 { return abs - (d.synAbs + 1) }

// deliveredOffset is the count of bytes emitted so far (0 until the ISN is
// known for this direction).
func (d *dirAssembler) deliveredOffset() uint64 {
	if !d.hasISN {
		return 0
	}
	return d.rcvNxt - (d.synAbs + 1)
}

func (d *dirAssembler) state() diag.SeqState {
	st := diag.SeqState{
		RcvNxtAbs:       d.rcvNxt,
		DeliveredOffset: d.deliveredOffset(),
		BufferedBytes:   d.buffered,
		FINSeen:         d.finSeen,
		FINEndAbs:       d.finAbs + 1,
		Closed:          d.finDone,
		Reset:           d.reset,
		Generation:      d.genIndex,
		InferredGen:     d.inferred,
	}
	if d.hasISN {
		st.ISN = d.isn
	}
	if !d.finSeen {
		st.FINEndAbs = 0
	}
	return st
}

// evidenceAt returns retained delivered bytes over [s,e) and whether the
// whole interval is verifiable from the evidence ring.
func (d *dirAssembler) evidenceAt(s, e uint64) ([]byte, bool) {
	if e <= s || d.evTail == 0 || !d.evSet {
		return nil, false
	}
	if s < d.evStart || e > d.evStart+uint64(len(d.evData)) {
		return nil, false
	}
	return d.evData[s-d.evStart : e-d.evStart], true
}

func (d *dirAssembler) appendEvidenceAt(absStart uint64, b []byte) {
	if d.evTail == 0 {
		return
	}
	if !d.evSet {
		d.evSet = true
		d.evStart = absStart
	}
	d.evData = append(d.evData, b...)
	if len(d.evData) > d.evTail {
		drop := uint64(len(d.evData) - d.evTail)
		d.evData = d.evData[drop:]
		d.evStart += drop
	}
}

func sha(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// differingEvents splits one detected non-equal overlap into events for the
// maximal runs of genuinely different bytes, so equal-byte overlap regions
// are never reported as conflicts.
func differingEvents(startAbs uint64, oldB, newB []byte, incRec, newRec string, delivered bool) []conflictEvent {
	var out []conflictEvent
	n := len(oldB)
	if len(newB) < n {
		n = len(newB)
	}
	for i := 0; i < n; {
		if oldB[i] == newB[i] {
			i++
			continue
		}
		j := i + 1
		for j < n && oldB[j] != newB[j] {
			j++
		}
		o, nb := oldB[i:j], newB[i:j]
		out = append(out, conflictEvent{
			startAbs: startAbs + uint64(i), endAbs: startAbs + uint64(j),
			incumbentRec: incRec, newcomerRec: newRec,
			incumbentSHA: sha(o), newcomerSHA: sha(nb),
			incumbentBytes: append([]byte(nil), o...),
			newcomerBytes:  append([]byte(nil), nb...),
			delivered:      delivered,
		})
		i = j
	}
	return out
}

// overlapRange returns the intersection of two half-open ranges.
func overlapRange(aStart, aEnd, bStart, bEnd uint64) (uint64, uint64) {
	lo := aStart
	if bStart > lo {
		lo = bStart
	}
	hi := aEnd
	if bEnd < hi {
		hi = bEnd
	}
	if lo >= hi {
		return 0, 0
	}
	return lo, hi
}

// insert merges the newcomer segment (absolute start, data, recordID).
//
// Returns the number of byte-identical duplicate bytes accepted as evidence
// and every contradictory range encountered. Conflict policy is applied
// immediately: first-wins discards the newcomer overlap, last-wins replaces
// buffered incumbent bytes, quarantine moves both copies to the poisoned
// area. Bytes already delivered are immutable under every policy.
func (d *dirAssembler) insert(start uint64, data []byte, recordID string) (dup int, conflicts []conflictEvent) {
	// segPart is one still-unhandled slice of the newcomer.
	type segPart = struct {
		start uint64
		data  []byte
	}

	// 1) Prefix already delivered: compare to the evidence ring.
	if start < d.rcvNxt {
		end := start + uint64(len(data))
		pe := end
		if pe > d.rcvNxt {
			pe = d.rcvNxt
		}
		old, ok := d.evidenceAt(start, pe)
		newPart := data[:pe-start]
		switch {
		case !ok:
			conflicts = append(conflicts, conflictEvent{
				startAbs: start, endAbs: pe,
				incumbentRec: "delivered-evicted", newcomerRec: recordID,
				incumbentSHA: "", newcomerSHA: sha(newPart), delivered: true,
				incumbentBytes: nil, newcomerBytes: append([]byte(nil), newPart...),
			})
		case bytes.Equal(old, newPart):
			dup += len(newPart)
		default:
			conflicts = append(conflicts, differingEvents(start, old, newPart,
				"delivered", recordID, true)...)
		}
		// Delivered bytes never enter the buffer.
		data = data[pe-start:]
		start = pe
	}

	var parts []segPart
	if len(data) > 0 {
		parts = append(parts, segPart{start, data})
	}

	// cutPart removes [s,e) from one part, returning its remnants.
	cutPart := func(p segPart, s, e uint64) []segPart {
		ps, pe := p.start, p.start+uint64(len(p.data))
		if e <= ps || s >= pe {
			return []segPart{p}
		}
		var out []segPart
		if s > ps {
			out = append(out, segPart{ps, p.data[:s-ps]})
		}
		if e < pe {
			out = append(out, segPart{e, p.data[e-ps:]})
		}
		return out
	}

	// 2) Quarantined overlap.
	for _, pb := range d.poisoned {
		var next []segPart
		for _, p := range parts {
			pEnd := p.start + uint64(len(p.data))
			ovS, ovE := overlapRange(p.start, pEnd, pb.start, pb.end())
			if ovS >= ovE {
				next = append(next, p)
				continue
			}
			oldPart, newPart := pb.slice(ovS, ovE), p.data[ovS-p.start:ovE-p.start]
			if bytes.Equal(oldPart, newPart) {
				dup += len(newPart)
			} else {
				conflicts = append(conflicts, differingEvents(ovS, oldPart, newPart,
					pb.recordID, recordID, false)...)
				if d.policy == config.PolicyLastWins {
					// Replace the held copy; the range stays poisoned.
					d.replacePoisoned(pb, ovS, ovE, newPart, recordID)
				}
			}
			// Newcomer bytes over a poisoned range never enter clean buffer.
			next = append(next, cutPart(p, ovS, ovE)...)
		}
		parts = next
	}

	// 3) Buffered overlap. Re-scan per surviving part; each pass either
	// consumes an overlap (removing bytes from a part) or leaves the part
	// alone, so the loop terminates.
	for {
		var hit *block
		var hitPart *segPart
		var ovS, ovE uint64
		for i := range parts {
			pEnd := parts[i].start + uint64(len(parts[i].data))
			for _, ob := range d.blocks {
				if cs, ce := overlapRange(parts[i].start, pEnd, ob.start, ob.end()); cs < ce {
					hit, hitPart, ovS, ovE = ob, &parts[i], cs, ce
					break
				}
			}
			if hit != nil {
				break
			}
		}
		if hit == nil {
			break
		}
		p := *hitPart
		oldPart, newPart := hit.slice(ovS, ovE), p.data[ovS-p.start:ovE-p.start]
		if bytes.Equal(oldPart, newPart) {
			dup += len(newPart)
		} else {
			conflicts = append(conflicts, differingEvents(ovS, oldPart, newPart,
				hit.recordID, recordID, false)...)
			switch d.policy {
			case config.PolicyFirstWins:
				// newcomer bytes discarded
			case config.PolicyLastWins:
				d.replaceBlockRange(hit, ovS, ovE, newPart, recordID)
			case config.PolicyQuarantine:
				d.poisoned = append(d.poisoned,
					&block{start: ovS, data: append([]byte(nil), oldPart...), recordID: "incumbent:" + hit.recordID},
					&block{start: ovS, data: append([]byte(nil), newPart...), recordID: recordID})
				d.deleteBlockRange(hit, ovS, ovE)
			}
		}
		// Remove the handled overlap from this part in-place.
		remnants := cutPart(p, ovS, ovE)
		var rebuilt []segPart
		for i := range parts {
			if &parts[i] == hitPart {
				rebuilt = append(rebuilt, remnants...)
			} else {
				rebuilt = append(rebuilt, parts[i])
			}
		}
		parts = rebuilt
		d.compact()
	}

	// 4) Surviving newcomer parts enter the buffer.
	for _, p := range parts {
		d.blocks = append(d.blocks, &block{
			start: p.start, data: append([]byte(nil), p.data...), recordID: recordID,
		})
	}
	d.compact()
	return dup, conflicts
}

// replacePoisoned swaps bytes [s,e) of poisoned block pb.
func (d *dirAssembler) replacePoisoned(pb *block, s, e uint64, newData []byte, recordID string) {
	var left, right []byte
	if s > pb.start {
		left = append([]byte(nil), pb.data[:s-pb.start]...)
	}
	if e < pb.end() {
		right = append([]byte(nil), pb.data[e-pb.start:]...)
	}
	var nb []*block
	if len(left) > 0 {
		nb = append(nb, &block{start: pb.start, data: left, recordID: pb.recordID})
	}
	nb = append(nb, &block{start: s, data: append([]byte(nil), newData...), recordID: recordID})
	if len(right) > 0 {
		nb = append(nb, &block{start: e, data: right, recordID: pb.recordID})
	}
	for i, x := range d.poisoned {
		if x == pb {
			d.poisoned = append(d.poisoned[:i], append(nb, d.poisoned[i+1:]...)...)
			return
		}
	}
}

// replaceBlockRange swaps buffered bytes [s,e) in (or around) b for newData.
func (d *dirAssembler) replaceBlockRange(b *block, s, e uint64, newData []byte, recordID string) {
	var left, right []byte
	if s > b.start {
		left = append([]byte(nil), b.data[:s-b.start]...)
	}
	if e < b.end() {
		right = append([]byte(nil), b.data[e-b.start:]...)
	}
	var nb []*block
	if len(left) > 0 {
		nb = append(nb, &block{start: b.start, data: left, recordID: b.recordID})
	}
	nb = append(nb, &block{start: s, data: append([]byte(nil), newData...), recordID: recordID})
	if len(right) > 0 {
		nb = append(nb, &block{start: e, data: right, recordID: b.recordID})
	}
	for i, x := range d.blocks {
		if x == b {
			d.blocks = append(d.blocks[:i], append(nb, d.blocks[i+1:]...)...)
			return
		}
	}
}

// deleteBlockRange removes [s,e) from b, dropping the block if it vanishes.
func (d *dirAssembler) deleteBlockRange(b *block, s, e uint64) {
	var left, right []byte
	if s > b.start {
		left = append([]byte(nil), b.data[:s-b.start]...)
	}
	if e < b.end() {
		right = append([]byte(nil), b.data[e-b.start:]...)
	}
	var nb []*block
	if len(left) > 0 {
		nb = append(nb, &block{start: b.start, data: left, recordID: b.recordID})
	}
	if len(right) > 0 {
		nb = append(nb, &block{start: e, data: right, recordID: b.recordID})
	}
	for i, x := range d.blocks {
		if x == b {
			d.blocks = append(d.blocks[:i], append(nb, d.blocks[i+1:]...)...)
			return
		}
	}
}

// compact sorts, merges abutting blocks with identical provenance and
// recounts buffer usage.
func (d *dirAssembler) compact() {
	sort.SliceStable(d.blocks, func(i, j int) bool { return d.blocks[i].start < d.blocks[j].start })
	var out []*block
	for _, b := range d.blocks {
		if b.end() <= b.start {
			continue
		}
		if len(out) > 0 {
			prev := out[len(out)-1]
			switch {
			case b.start < prev.end():
				continue // leftover overlap: never overwrite evidence
			case b.start == prev.end() && b.recordID == prev.recordID:
				prev.data = append(prev.data, b.data...)
				continue
			}
		}
		out = append(out, b)
	}
	d.blocks = out
	d.buffered = 0
	for _, b := range d.blocks {
		d.buffered += len(b.data)
	}
	for _, b := range d.poisoned {
		d.buffered += len(b.data)
	}
}

// overBufferLimit reports whether newBytes would exceed the cap.
func (d *dirAssembler) overBufferLimit(newBytes int) bool {
	return d.maxBuffer > 0 && d.buffered+newBytes > d.maxBuffer
}
