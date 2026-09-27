package reassembly

import "context"

// emit is one contiguous delivery produced by the delivery loop.
type emit struct {
	off      uint64 // stream offset
	data     []byte
	recordID string
}

// gapDelta describes a gap whose open/fill status changed.
type gapDelta struct {
	startOff, endOff uint64
	opened           bool
	filled           bool
	fillRecord       string
}

// deliver emits every contiguous, evidence-backed run starting at rcvNxt.
//
// Poisoned ranges block delivery at their left edge even if later clean
// bytes exist — those bytes are evidence but not a contiguous stream prefix.
// It returns the emitted runs, the hole intervals visible after delivery
// (opened), and whether the delivered frontier advanced (which fills all
// gaps ending at or before the new frontier).
func (d *dirAssembler) deliver(_ context.Context, fillRecordHint string) (emits []emit, deltas []gapDelta) {
	oldRcv := d.rcvNxt

	frontPoison := func() (uint64, bool) {
		var at uint64
		found := false
		for _, pb := range d.poisoned {
			if pb.end() <= d.rcvNxt {
				continue
			}
			s := pb.start
			if s < d.rcvNxt {
				s = d.rcvNxt
			}
			if !found || s < at {
				at, found = s, true
			}
		}
		return at, found
	}

	for {
		var b *block
		for _, cand := range d.blocks {
			if cand.start <= d.rcvNxt && cand.end() > d.rcvNxt {
				b = cand
				break
			}
		}
		if b == nil {
			break
		}
		limit := b.end()
		for _, pb := range d.poisoned {
			if pb.start >= d.rcvNxt && pb.start < limit {
				limit = pb.start
			}
		}
		chunk := b.slice(d.rcvNxt, limit)
		off := d.off(d.rcvNxt)
		emits = append(emits, emit{off: off, data: append([]byte(nil), chunk...), recordID: b.recordID})
		d.appendEvidenceAt(d.rcvNxt, chunk)
		d.rcvNxt = limit
		d.deleteBlockRange(b, b.start, limit)
		if at, blocked := frontPoison(); blocked && at <= d.rcvNxt {
			break
		}
	}

	// FIN bookkeeping: the FIN's own sequence becomes deliverable once all
	// preceding bytes are in. It emits no bytes but closes the direction.
	if d.finSeen && !d.finDone && d.rcvNxt == d.finAbs {
		d.finDone = true
	}

	if d.rcvNxt > oldRcv {
		deltas = append(deltas, gapDelta{
			startOff: d.off(oldRcv), endOff: d.off(d.rcvNxt),
			filled: true, fillRecord: fillRecordHint,
		})
	}

	// Describe the hole immediately ahead of the frontier.
	if !d.finDone {
		// Clean block beyond a hole: missing bytes are [rcvNxt, block.start).
		var blockStart uint64
		haveBlock := false
		for _, cand := range d.blocks {
			if cand.start > d.rcvNxt {
				if !haveBlock || cand.start < blockStart {
					blockStart, haveBlock = cand.start, true
				}
			}
		}
		// Poisoned range at/after the frontier: the held bytes themselves
		// are an undecidable interval [max(rcvNxt,pb.start), pb.end).
		var poisonS, poisonE uint64
		havePoison := false
		for _, pb := range d.poisoned {
			if pb.end() <= d.rcvNxt {
				continue
			}
			s := pb.start
			if s < d.rcvNxt {
				s = d.rcvNxt
			}
			if !havePoison || s < poisonS {
				poisonS, poisonE, havePoison = s, pb.end(), true
			}
		}
		// Choose the nearer boundary: a poison range starting exactly at
		// rcvNxt blocks immediately and defines the whole held interval;
		// otherwise a clean block ahead bounds a plain missing-byte gap.
		gapStart, gapEnd := d.rcvNxt, uint64(0)
		switch {
		case havePoison && poisonS == d.rcvNxt:
			gapEnd = poisonE
		case haveBlock && (!havePoison || blockStart <= poisonS):
			gapEnd = blockStart
		case havePoison:
			gapEnd = poisonS
		case d.finSeen:
			gapEnd = d.finAbs
		}
		if gapEnd > gapStart {
			deltas = append(deltas, gapDelta{
				startOff: d.off(gapStart), endOff: d.off(gapEnd),
				opened: true,
			})
		}
	}
	d.compact()
	return emits, deltas
}
