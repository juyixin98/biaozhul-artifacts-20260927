package reassembly

import (
	"fmt"

	"tcpreplay/internal/netmodel"
)

// Process feeds one observed packet to the state machine in arrival order.
// requestID correlates all emitted diagnostics with the ingest request;
// recordID (from the packet, or assigned by the caller) identifies the
// evidence in conflicts.
func (m *Manager) Process(pk netmodel.Packet, requestID, recordID string) ProcessResult {
	m.mu.Lock()
	defer m.mu.Unlock()

	k, aToB := pk.FlowKeyAndDir()
	ts := pk.Timestamp
	g, genEvents := m.latestOrNewGeneration(pk, k, aToB, requestID, ts)
	d := g.dir(aToB)

	res := ProcessResult{GenerationIndex: g.Index, Accepted: true}
	if aToB {
		res.Direction = "a_to_b"
	} else {
		res.Direction = "b_to_a"
	}
	res.Events = append(res.Events, genEvents...)

	switch {
	case pk.Has(netmodel.FlagSYN) && pk.Has(netmodel.FlagACK):
		res.Events = append(res.Events, m.handleSYNACK(requestID, recordID, pk, g, d, aToB, ts))
	case pk.Has(netmodel.FlagSYN):
		m.handleSYN(pk, g, d, aToB)
	}

	if pk.Has(netmodel.FlagRST) {
		res.Events = append(res.Events, m.handleRST(requestID, recordID, pk, g, d, aToB, ts))
	}

	if len(pk.Payload) > 0 {
		put, evs, confs := m.handleData(requestID, recordID, pk, g, d, aToB, ts)
		res.Put = put
		res.Events = append(res.Events, evs...)
		res.Conflicts = append(res.Conflicts, confs...)
		m.conflicts = append(m.conflicts, confs...)
	}

	if pk.Has(netmodel.FlagFIN) {
		evs := m.handleFIN(requestID, recordID, pk, g, d, aToB, ts)
		res.Events = append(res.Events, evs...)
	}

	if g.closed && !res.FlowClosed {
		// Mark the completion transition exactly once.
		if bothDirectionsClosed(g) {
			res.FlowClosed = true
			res.Events = append(res.Events, m.emit(requestID, recordID, ts, g, aToB,
				EvGenerationComplete, LevelInfo,
				fmt.Sprintf("generation #%d complete (both directions closed)", g.Index), d, pk.Seq, 0, 0))
		}
	}
	return res
}

// handleSYN anchors the SYN control byte for the opening direction. The SYN
// consumes one sequence number: it sits at coordinate 0 of the direction and
// the first data byte is at coordinate 1. Diagnostic events for SYNs are
// emitted by the generation-resolution logic (SYN_OPENED / SYN_DUPLICATE /
// NEW_GENERATION), so this function only mutates state.
func (m *Manager) handleSYN(pk netmodel.Packet, g *Generation, d *DirectionState, aToB bool) {
	if !d.anchored {
		d.mapper.AddAnchor(pk.Seq, 0)
		d.base = 1
		d.firstObservedSeq = pk.Seq
		d.handshakeKnown = true
		d.anchored = true
	}
	if !g.rolesSet {
		g.clientIsA = aToB
		g.rolesSet = true
	}
}

// handleSYNACK processes the server's SYN+ACK: it establishes the connection
// and anchors the reverse direction the same way.
func (m *Manager) handleSYNACK(reqID, recordID string, pk netmodel.Packet, g *Generation, d *DirectionState, aToB bool, ts string) Event {
	if !d.anchored {
		d.mapper.AddAnchor(pk.Seq, 0)
		d.base = 1
		d.firstObservedSeq = pk.Seq
		d.handshakeKnown = true
		d.anchored = true
	}
	if !g.established {
		g.established = true
	}
	return m.emit(reqID, recordID, ts, g, aToB, EvSYNACKEstablished, LevelInfo,
		"SYN+ACK anchors reverse direction; connection established", d, pk.Seq, 0, 0)
}

// handleRST closes the generation; later packets on the same 4-tuple cannot be
// attributed until a new SYN reopens it.
func (m *Manager) handleRST(reqID, recordID string, pk netmodel.Packet, g *Generation, d *DirectionState, aToB bool, ts string) Event {
	d.reset = true
	g.reset = true
	g.closed = true
	return m.emit(reqID, recordID, ts, g, aToB, EvRSTClosed, LevelInfo,
		"RST closes generation; subsequent data is unattributable until a new SYN", d, pk.Seq, 0, 0)
}

// handleData maps the payload into monotonic coordinates and inserts it under
// the configured policy, refusing data at or past an already-seen FIN.
func (m *Manager) handleData(reqID, recordID string, pk netmodel.Packet, g *Generation, d *DirectionState, aToB bool, ts string) (PutResult, []Event, []Conflict) {
	// Anchorless direction (no handshake): the first observed data byte
	// initially defines relative zero; if a segment that claims earlier
	// sequence numbers arrives later, the origin moves to it. All stored
	// coordinates are internal monotonic values, and views subtract the base,
	// so shifting the base never moves or duplicates accepted bytes.
	if !d.anchored {
		abs, _ := d.mapper.AbsWith(pk.Seq)
		d.base = abs
		d.firstObservedSeq = pk.Seq
		d.anchored = true
	}
	start := d.mapper.Abs(pk.Seq)
	if !d.handshakeKnown && start < d.base {
		d.base = start
	}
	end := start + int64(len(pk.Payload))

	// Data after FIN: the FIN consumed the final sequence number, so no data
	// byte may occupy finPos or above.
	if d.finSeen && start >= d.finPos {
		ev := m.emit(reqID, recordID, ts, g, aToB, EvDataAfterFIN, LevelReject,
			fmt.Sprintf("data [%d,%d) at/after FIN position %d rejected", start-d.base, end-d.base, d.finPos-d.base),
			d, pk.Seq, start-d.base, end-d.base)
		if m.preview {
			ev.PayloadPreview, ev.PreviewTotal = maskPreview(pk.Payload)
		}
		return PutResult{Start: start, End: end}, []Event{ev}, nil
	}

	if g.reset {
		ev := m.emit(reqID, recordID, ts, g, aToB, EvPacketAfterRST, LevelUndecided,
			"data arrives after RST without a new SYN; not output", d, pk.Seq, start-d.base, end-d.base)
		return PutResult{Start: start, End: end}, []Event{ev}, nil
	}

	rawSeqAt := func(abs int64) uint32 {
		return netmodel.SeqAdd(pk.Seq, abs-start)
	}
	put, byteConfs := d.buf.put(start, pk.Payload, recordID, m.policy, rawSeqAt)

	var events []Event
	var conflicts []Conflict
	if put.ConflictBytes > 0 {
		for _, bc := range byteConfs {
			m.conflictSeq++
			cid := fmt.Sprintf("conf-%06d", m.conflictSeq)
			disp := bc.disposition
			if m.policy == PolicyQuarantine {
				d.held = append(d.held, HeldByte{
					Offset: bc.offset, RawSeq: bc.rawSeq, Value: bc.offered, RecordID: recordID,
				})
			}
			c := Conflict{
				ID: cid, RequestID: reqID, RecordID: recordID, Flow: g.Flow.String(),
				Generation: g.Index, Direction: dirName(aToB), ByteOffset: bc.offset,
				RawSeq: bc.rawSeq, Accepted: bc.existing, Offered: bc.offered,
				AcceptedBy: bc.existingOwner, Policy: m.policy, Disposition: disp, Timestamp: ts,
			}
			conflicts = append(conflicts, c)
		}
		ev := m.emit(reqID, recordID, ts, g, aToB, EvOverlapConflict, LevelWarn,
			fmt.Sprintf("overlap conflict over %d byte(s) at [%d,%d); policy=%s disposition: %s",
				put.ConflictBytes, start-d.base, end-d.base, m.policy, summarizeDispositions(byteConfs)),
			d, pk.Seq, start-d.base, end-d.base)
		events = append(events, ev)
	}
	switch {
	case put.NewBytes > 0:
		ev := m.emit(reqID, recordID, ts, g, aToB, EvSegmentAccepted, LevelInfo,
			fmt.Sprintf("accepted %d new byte(s) [%d,%d); %d identical retransmitted",
				put.NewBytes, start-d.base, end-d.base, put.IdenticalBytes),
			d, pk.Seq, start-d.base, end-d.base)
		if m.preview {
			ev.PayloadPreview, ev.PreviewTotal = maskPreview(pk.Payload)
		}
		events = append(events, ev)
	case put.IdenticalBytes > 0:
		events = append(events, m.emit(reqID, recordID, ts, g, aToB, EvRetransmitIdent, LevelInfo,
			fmt.Sprintf("identical retransmission of %d byte(s) [%d,%d); not duplicated",
				put.IdenticalBytes, start-d.base, end-d.base),
			d, pk.Seq, start-d.base, end-d.base))
	}
	return put, events, conflicts
}

// handleFIN consumes the final sequence number of the direction.
func (m *Manager) handleFIN(reqID, recordID string, pk netmodel.Packet, g *Generation, d *DirectionState, aToB bool, ts string) []Event {
	// FIN position = seq + payload length. Anchor it even on a bare FIN so
	// wrap mapping stays unambiguous.
	finRaw := netmodel.SeqAdd(pk.Seq, int64(len(pk.Payload)))
	if !d.anchored {
		// FIN with no prior data and no observed SYN: anchor relative zero.
		abs, _ := d.mapper.AbsWith(pk.Seq)
		d.base = abs
		d.firstObservedSeq = pk.Seq
		d.anchored = true
	}
	if !d.handshakeKnown {
		// A bare FIN may be the earliest thing observed in this direction.
		if start := d.mapper.Abs(pk.Seq); start < d.base {
			d.base = start
		}
	}
	finPos := d.mapper.Abs(finRaw)

	if d.finSeen {
		if finPos == d.finPos {
			return []Event{m.emit(reqID, recordID, ts, g, aToB, EvFINDuplicate, LevelInfo,
				"duplicate FIN at same position; retransmission", d, finRaw, finPos-d.base, finPos-d.base+1)}
		}
		return []Event{m.emit(reqID, recordID, ts, g, aToB, EvFINConflict, LevelUndecided,
			fmt.Sprintf("FIN at %d disagrees with established FIN at %d", finPos-d.base, d.finPos-d.base),
			d, finRaw, finPos-d.base, finPos-d.base+1)}
	}
	d.finSeen = true
	d.finPos = finPos
	ev := m.emit(reqID, recordID, ts, g, aToB, EvFINAccepted, LevelInfo,
		fmt.Sprintf("FIN accepted at position %d (consumes 1 sequence number)", finPos-d.base),
		d, finRaw, finPos-d.base, finPos-d.base+1)
	// Mark the generation closed if both directions have FINed (half-close is
	// represented by exactly one finSeen and stays readable meanwhile).
	if bothDirectionsClosed(g) {
		g.closed = true
	}
	return []Event{ev}
}

func bothDirectionsClosed(g *Generation) bool {
	return g.AtoB != nil && g.BtoA != nil && g.AtoB.finSeen && g.BtoA.finSeen
}

func dirName(aToB bool) string {
	if aToB {
		return "a_to_b"
	}
	return "b_to_a"
}

func summarizeDispositions(cs []byteConflict) string {
	seen := map[string]bool{}
	var out string
	for _, c := range cs {
		if !seen[c.disposition] {
			seen[c.disposition] = true
			if out != "" {
				out += ","
			}
			out += c.disposition
		}
	}
	return out
}
