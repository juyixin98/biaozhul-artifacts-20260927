package reassembly

import (
	"context"
	"fmt"
	"sync"

	"tcpreasm/internal/config"
	"tcpreasm/internal/diag"
	"tcpreasm/internal/reassembly/seqnum"
	"tcpreasm/internal/store"
	"tcpreasm/internal/tcpmodel"
)

// captureSource is the provenance tag for packets entered via the capture
// ingestion path (HTTP JSONL/JSON or the replay command).
const captureSource = "capture"

// Engine is the concurrency-safe reassembler. All observed packets enter
// through Process; results and diagnostics are produced there and state is
// persisted through the configured Persister.
type Engine struct {
	mu     sync.Mutex
	opts   Options
	conns  map[string]*connState
	ingest int64
}

type connState struct {
	flow   tcpmodel.FlowKey
	client tcpmodel.Endpoint // SYN source; zero until known
	gens   []*generation
}

// NewEngine builds an engine.
func NewEngine(opts Options) *Engine {
	return &Engine{opts: opts, conns: map[string]*connState{}}
}

// Stats are coarse counters returned with an ingestion response.
type Stats struct {
	Connections int   `json:"connections"`
	Generations int   `json:"generations"`
	IngestSeq   int64 `json:"ingest_seq"`
}

// Stats returns current engine counters.
func (e *Engine) Stats() Stats {
	e.mu.Lock()
	defer e.mu.Unlock()
	n := 0
	for _, c := range e.conns {
		n += len(c.gens)
	}
	return Stats{Connections: len(e.conns), Generations: n, IngestSeq: e.ingest}
}

// ensureIngestSeq takes the next monotonic sequence from the store when one
// exists; otherwise the engine keeps its in-process counter.
func (e *Engine) ensureIngestSeq(ctx context.Context) (int64, error) {
	if e.opts.Store != nil {
		n, err := e.opts.Store.NextIngestSeq(ctx)
		if err != nil {
			return 0, err
		}
		if n > e.ingest {
			e.ingest = n
			return n, nil
		}
	}
	e.ingest++
	return e.ingest, nil
}

// Process ingests one packet in observation order. The request id is stamped
// on every diagnostic it produces.
func (e *Engine) Process(ctx context.Context, p tcpmodel.Packet, requestID string) (ProcessResult, error) {
	e.mu.Lock()
	defer e.mu.Unlock()

	seq, err := e.ensureIngestSeq(ctx)
	if err != nil {
		return ProcessResult{}, err
	}

	res := ProcessResult{RequestID: requestID}
	flow, err := p.Flow()
	if err != nil {
		return e.rejectMalformed(ctx, p, requestID, seq, err)
	}
	res.FlowKey = flow.KeyStr

	// Idempotent capture replay.
	source := captureSource
	if e.opts.Store != nil {
		if p.RecordID != "" {
			exists, err := e.opts.Store.PacketExists(ctx, source, p.RecordID)
			if err != nil {
				return res, err
			}
			if exists {
				res.Decision = diag.Rejected
				res.Category = diag.CatBadPacket
				res.Reason = "duplicate record_id already ingested"
				res.DuplicatePacket = true
				return res, nil
			}
		}
	}

	cs := e.conns[flow.KeyStr]

	switch {
	case p.RST:
		return e.processRST(ctx, cs, flow, p, requestID, seq)
	case p.SYN && p.ACK:
		return e.processSYNACK(ctx, cs, flow, p, requestID, seq)
	case p.SYN:
		return e.processSYN(ctx, cs, flow, p, requestID, seq)
	default:
		return e.processSegment(ctx, cs, flow, p, requestID, seq)
	}
}

// ---- SYN / SYN-ACK ----------------------------------------------------------------

func (e *Engine) processSYN(ctx context.Context, cs *connState, flow tcpmodel.FlowKey,
	p tcpmodel.Packet, requestID string, seq int64) (ProcessResult, error) {

	res := newResult(flow.KeyStr, requestID)
	res.SegSeqAbs = uint64(p.Seq)
	src := tcpmodel.Endpoint{IP: p.SrcIP, Port: p.SrcPort}

	// New flow: this SYN is the opening handshake.
	if cs == nil {
		cs = &connState{flow: flow, client: src}
		g := newGeneration(flow.KeyStr, 1, false, e.opts.Cfg)
		g.birth = seq
		g.connState = stSynSent
		d := g.c2s
		d.hasISN, d.isn, d.inferred = true, p.Seq, false
		d.synAbs = uint64(p.Seq)
		d.rcvNxt = d.synAbs + 1
		cs.gens = append(cs.gens, g)
		e.conns[flow.KeyStr] = cs
		// A SYN may legally carry data (TCP Fast Open); evidence only,
		// buffered at the post-SYN sequence.
		dr := e.acceptHandshakeData(ctx, g, d, p, requestID, seq)
		e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Accepted, diag.CatHandshakeSYN,
			"opening SYN establishes generation 1"))
		res.DirResults = append(res.DirResults, dr)
		res.Decision, res.Category = diag.Accepted, diag.CatHandshakeSYN
		res.GenIndex = g.index
		e.persistPacket(ctx, p, g.index, string(tcpmodel.DirC2S), p.Seq, p.Seq+1, "ACCEPTED", string(diag.CatHandshakeSYN), seq)
		e.persistConn(ctx, cs, seq)
		return res, nil
	}

	// Existing flow.
	last := cs.gens[len(cs.gens)-1]

	// Retransmitted opening SYN (same client endpoint, identical ISN) while
	// the generation it opened is still live: evidence, no new generation,
	// no second consumption of the SYN sequence.
	if tcpmodel.CompareEndpoints(src, cs.client) == 0 && last.c2s.hasISN &&
		p.Seq == last.c2s.isn && !last.isClosedOrReset() {
		d := last.c2s
		dr := DirectionResult{Direction: string(tcpmodel.DirC2S), Accepted: true}
		if len(p.Payload) > 0 {
			dup, _ := d.insert(d.synAbs+1, p.Payload, p.RecordID)
			emits, deltas := d.deliver(ctx, p.RecordID)
			e.afterInsert(ctx, last, d, dup, nil, emits, deltas, p, requestID, seq, &dr)
		}
		res.Decision, res.Category, res.GenIndex = diag.Accepted, diag.CatRetransmitIdentical, last.index
		res.Reason = "identical retransmission of the opening SYN; sequence consumed once"
		e.emit(ctx, e.baseRec(p, res, last, d, requestID, diag.Accepted, diag.CatRetransmitIdentical, res.Reason))
		res.DirResults = append(res.DirResults, dr)
		e.persistPacket(ctx, p, last.index, string(tcpmodel.DirC2S), p.Seq, p.Seq+1, "ACCEPTED", string(diag.CatRetransmitIdentical), seq)
		return res, nil
	}

	reuse := last.isClosedOrReset()
	if !reuse {
		res.Decision, res.Category = diag.Rejected, diag.CatRepeatedSYN
		res.Reason = "SYN on a still-open generation with a different ISN does not begin reuse"
		res.GenIndex = last.index
		d := last.c2s
		e.emit(ctx, e.baseRec(p, res, last, d, requestID, diag.Rejected, diag.CatRepeatedSYN, res.Reason))
		e.persistPacket(ctx, p, last.index, string(tcpmodel.DirC2S), p.Seq, p.Seq+1, "REJECTED", string(diag.CatRepeatedSYN), seq)
		return res, nil
	}

	// Reused connection: new generation. If the client endpoint flips,
	// re-orient the generation.
	g := newGeneration(flow.KeyStr, last.index+1, false, e.opts.Cfg)
	g.birth = seq
	g.connState = stSynSent
	if tcpmodel.CompareEndpoints(src, cs.client) != 0 {
		cs.client = src
	}
	d := g.c2s
	d.hasISN, d.isn = true, p.Seq
	d.synAbs = uint64(p.Seq)
	d.rcvNxt = d.synAbs + 1
	cs.gens = append(cs.gens, g)
	dr := e.acceptHandshakeData(ctx, g, d, p, requestID, seq)
	e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Accepted, diag.CatGenerationReuse,
		fmt.Sprintf("new SYN after generation %d closed opens generation %d", last.index, g.index)))
	res.DirResults = append(res.DirResults, dr)
	res.Decision, res.Category, res.GenIndex = diag.Accepted, diag.CatGenerationReuse, g.index
	e.persistPacket(ctx, p, g.index, string(tcpmodel.DirC2S), p.Seq, p.Seq+1, "ACCEPTED", string(diag.CatGenerationReuse), seq)
	e.persistConn(ctx, cs, seq)
	return res, nil
}

func (e *Engine) processSYNACK(ctx context.Context, cs *connState, flow tcpmodel.FlowKey,
	p tcpmodel.Packet, requestID string, seq int64) (ProcessResult, error) {

	res := newResult(flow.KeyStr, requestID)
	res.SegSeqAbs = uint64(p.Seq)
	if cs == nil || len(cs.gens) == 0 {
		return e.rejectNoFlow(ctx, p, res, requestID, seq, diag.CatStraySYNACK,
			"SYN/ACK has no preceding SYN for its 4-tuple")
	}
	g := cs.gens[len(cs.gens)-1]
	d := g.s2c
	if g.connState != stSynSent || d.hasISN {
		res.Decision, res.Category, res.Reason = diag.Rejected, diag.CatStraySYNACK,
			"SYN/ACK does not match a generation awaiting the handshake response"
		res.GenIndex = g.index
		e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Rejected, diag.CatStraySYNACK, res.Reason))
		e.persistPacket(ctx, p, g.index, string(tcpmodel.DirS2C), p.Seq, p.Seq+1, "REJECTED", string(diag.CatStraySYNACK), seq)
		return res, nil
	}
	d.hasISN, d.isn = true, p.Seq
	// Lift the responder ISN against the initiator's SYN as the window
	// hint; responder and initiator initial sequence numbers are usually
	// far apart in 32-bit space but within half a space of one another in
	// real captures, so pick the representative nearest the c2s SYN.
	d.synAbs = seqnum.Extend(p.Seq, g.c2s.synAbs)
	d.rcvNxt = d.synAbs + 1
	g.connState = stEstablished
	dr := e.acceptHandshakeData(ctx, g, d, p, requestID, seq)
	e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Accepted, diag.CatHandshakeSYNACK,
		"SYN/ACK completes responder side of handshake"))
	res.DirResults = append(res.DirResults, dr)
	res.Decision, res.Category, res.GenIndex = diag.Accepted, diag.CatHandshakeSYNACK, g.index
	e.persistPacket(ctx, p, g.index, string(tcpmodel.DirS2C), p.Seq, p.Seq+1, "ACCEPTED", string(diag.CatHandshakeSYNACK), seq)
	e.persistConn(ctx, cs, seq)
	return res, nil
}

// acceptHandshakeData buffers/ delivers payload carried on a SYN or SYN/ACK
// (rare but legal). It reuses the normal segment path with seq+1.
func (e *Engine) acceptHandshakeData(ctx context.Context, g *generation, d *dirAssembler,
	p tcpmodel.Packet, requestID string, ingestSeq int64) (dr DirectionResult) {

	dr.Direction = d.direction
	if len(p.Payload) == 0 {
		return dr
	}
	dataStart := d.synAbs + 1
	absStart := dataStart
	dup, conflicts := d.insert(absStart, p.Payload, p.RecordID)
	emits, deltas := d.deliver(ctx, p.RecordID)
	e.afterInsert(ctx, g, d, dup, conflicts, emits, deltas, p, requestID, ingestSeq, &dr)
	return dr
}

// ---- RST -------------------------------------------------------------------------

func (e *Engine) processRST(ctx context.Context, cs *connState, flow tcpmodel.FlowKey,
	p tcpmodel.Packet, requestID string, seq int64) (ProcessResult, error) {

	res := newResult(flow.KeyStr, requestID)
	if cs == nil || len(cs.gens) == 0 {
		return e.rejectNoFlow(ctx, p, res, requestID, seq, diag.CatUnknownFlow,
			"RST for unknown 4-tuple")
	}
	g, dir, reason, ok := e.bindGeneration(cs, flow, p, true)
	if !ok {
		res.Decision, res.Category, res.Reason = diag.Undecidable, diag.CatGenerationAmbiguous, reason
		e.emit(ctx, diag.Record{
			RequestID: requestID, RecordID: p.RecordID, FlowKey: flow.KeyStr,
			Decision: diag.Undecidable, Category: diag.CatGenerationAmbiguous, Reason: reason,
			State: diag.SeqState{Generation: 0},
			Src:   p.SrcIP.String() + ":0", Dst: p.DstIP.String() + ":0",
		})
		e.persistPacket(ctx, p, 0, "", 0, 0, "UNDECIDABLE", string(diag.CatGenerationAmbiguous), seq)
		return res, nil
	}
	d := g.dir(dir)
	d.reset = true
	res.Decision, res.Category, res.GenIndex = diag.Accepted, diag.CatRST, g.index
	res.Direction = string(dir)
	e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Accepted, diag.CatRST,
		"RST marks generation reset; later bytes are refused"))
	e.persistPacket(ctx, p, g.index, string(dir), p.Seq, p.Seq, "ACCEPTED", string(diag.CatRST), seq)
	e.persistConn(ctx, cs, seq)
	return res, nil
}

// ---- data / ACK / FIN segments ----------------------------------------------------

func (e *Engine) processSegment(ctx context.Context, cs *connState, flow tcpmodel.FlowKey,
	p tcpmodel.Packet, requestID string, seq int64) (ProcessResult, error) {

	res := newResult(flow.KeyStr, requestID)

	// Pure ACK/keepalive with no flow: reject as unroutable.
	if cs == nil || len(cs.gens) == 0 {
		if len(p.Payload) == 0 && !p.FIN {
			return e.rejectNoFlow(ctx, p, res, requestID, seq, diag.CatUnknownFlow,
				"bare ACK for unknown 4-tuple")
		}
		return e.processInferred(ctx, flow, p, requestID, seq)
	}

	g, dir, reason, ok := e.bindGeneration(cs, flow, p, false)
	if !ok {
		res.Decision, res.Category, res.Reason = diag.Undecidable, diag.CatGenerationAmbiguous, reason
		e.emit(ctx, diag.Record{
			RequestID: requestID, RecordID: p.RecordID, FlowKey: flow.KeyStr,
			Decision: diag.Undecidable, Category: diag.CatGenerationAmbiguous, Reason: reason,
			State: diag.SeqState{},
		})
		e.persistPacket(ctx, p, 0, "", 0, 0, "UNDECIDABLE", string(diag.CatGenerationAmbiguous), seq)
		return res, nil
	}
	d := g.dir(dir)
	res.GenIndex, res.Direction = g.index, string(dir)

	if d.reset {
		res.Decision, res.Category, res.Reason = diag.Rejected, diag.CatAfterRST,
			"segment arrives after RST for this direction"
		e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Rejected, diag.CatAfterRST, res.Reason))
		e.persistPacket(ctx, p, g.index, string(dir), p.Seq, p.Seq+uint32(len(p.Payload)), "REJECTED", string(diag.CatAfterRST), seq)
		return res, nil
	}

	// No data, no FIN: keepalive/window update.
	if len(p.Payload) == 0 && !p.FIN {
		res.Decision, res.Category = diag.Accepted, diag.CatKeepalive
		res.Reason = "bare ACK updates no byte state"
		e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Accepted, diag.CatKeepalive, res.Reason))
		e.persistPacket(ctx, p, g.index, string(dir), p.Seq, p.Seq, "ACCEPTED", string(diag.CatKeepalive), seq)
		return res, nil
	}

	// Determine absolute data range. With a known ISN, lift using rcvNxt as
	// the window hint. Without one, infer the generation epoch here.
	if !d.hasISN {
		// Should only happen on inferred generations (bind permits it).
		d.inferred = true
		d.hasISN = true
		d.isn = p.Seq - 1 // virtual ISN: first byte is stream offset 0
		d.synAbs = uint64(p.Seq) + seqnum.Space - 1
		d.rcvNxt = d.synAbs + 1
	}
	hint := d.rcvNxt
	if d.finSeen {
		hint = d.finAbs
	}
	absStart := seqnum.Extend(p.Seq, hint)
	absEnd := absStart + uint64(len(p.Payload))
	res.SegSeqAbs, res.SegEndAbs = absStart, absEnd

	// FIN handling: FIN consumes the sequence right after data.
	if p.FIN {
		finAbs := absEnd
		if d.finSeen {
			if d.finAbs != finAbs {
				res.Decision, res.Category, res.Reason = diag.Rejected, diag.CatDataAfterFIN,
					"second FIN disagrees on closing sequence"
				e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Rejected, diag.CatDataAfterFIN, res.Reason))
				e.persistPacket(ctx, p, g.index, string(dir), p.Seq, p.Seq+uint32(p.SegmentLen()), "REJECTED", string(diag.CatDataAfterFIN), seq)
				return res, nil
			}
		} else {
			d.finSeen = true
			d.finAbs = finAbs
			d.finRecord = p.RecordID
		}
	}

	// Data after FIN: refuse bytes whose data range starts at or beyond the
	// FIN sequence. Data before/including finAbs-1 is retransmission of the
	// pre-FIN stream and handled normally below.
	if d.finSeen && len(p.Payload) > 0 && absStart >= d.finAbs {
		res.Decision, res.Category, res.Reason = diag.Rejected, diag.CatDataAfterFIN,
			fmt.Sprintf("data starts at abs %d at/after FIN at %d", absStart, d.finAbs)
		e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Rejected, diag.CatDataAfterFIN, res.Reason))
		e.persistPacket(ctx, p, g.index, string(dir), p.Seq, p.Seq+uint32(p.SegmentLen()), "REJECTED", string(diag.CatDataAfterFIN), seq)
		return res, nil
	}

	// Window acceptance: segments more than half a sequence space away from
	// the learned ISN are not believable from this vantage point.
	if dist := seqnum.Sub(p.Seq, d.isn); uint64(absDist(dist)) > seqnum.Space/2 {
		res.Decision, res.Category, res.Reason = diag.Rejected, diag.CatOutsideWindow,
			fmt.Sprintf("seq %d is more than half a sequence space from ISN %d", p.Seq, d.isn)
		e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Rejected, diag.CatOutsideWindow, res.Reason))
		e.persistPacket(ctx, p, g.index, string(dir), p.Seq, p.Seq+uint32(p.SegmentLen()), "REJECTED", string(diag.CatOutsideWindow), seq)
		return res, nil
	}

	// Buffer cap.
	if d.overBufferLimit(len(p.Payload)) {
		res.Decision, res.Category, res.Reason = diag.Rejected, diag.CatBufferLimit,
			fmt.Sprintf("buffered %d + %d > cap %d", d.buffered, len(p.Payload), d.maxBuffer)
		e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Rejected, diag.CatBufferLimit, res.Reason))
		e.persistPacket(ctx, p, g.index, string(dir), p.Seq, p.Seq+uint32(p.SegmentLen()), "REJECTED", string(diag.CatBufferLimit), seq)
		return res, nil
	}

	dup, conflicts := d.insert(absStart, p.Payload, p.RecordID)
	emits, deltas := d.deliver(ctx, p.RecordID)

	dr := DirectionResult{Direction: string(dir), BytesDedup: dup, Accepted: true}
	category := diag.CatInOrderData
	if len(p.Payload) > 0 {
		switch {
		case len(conflicts) > 0:
			category = conflictCategory(conflicts)
		case dup > 0:
			category = diag.CatRetransmitIdentical
		case len(emits) == 0:
			category = diag.CatOutOfOrderData
		}
	}
	// FIN is the headline category only for the segment that actually
	// completes the close, not for later retransmissions.
	if p.FIN && d.finDone && len(conflicts) == 0 && dup == 0 {
		category = diag.CatFIN
	}
	e.afterInsert(ctx, g, d, dup, conflicts, emits, deltas, p, requestID, seq, &dr)
	res.DirResults = append(res.DirResults, dr)
	res.Decision = diag.Accepted
	if hasUndeliveredConflict(conflicts) {
		res.Decision = diag.Undecidable
	}
	res.Category = category
	e.emit(ctx, e.baseRec(p, res, g, d, requestID, res.Decision, category,
		verdictReason(dup, conflicts, emits, dr)))
	e.persistPacket(ctx, p, g.index, string(dir), p.Seq, p.Seq+uint32(p.SegmentLen()),
		string(res.Decision), string(category), seq)
	e.persistConn(ctx, cs, seq)
	return res, nil
}

// processInferred handles data on an unseen 4-tuple ("missing handshake"
// fixtures). It either opens an explicitly inferred generation or rejects.
func (e *Engine) processInferred(ctx context.Context, flow tcpmodel.FlowKey,
	p tcpmodel.Packet, requestID string, seq int64) (ProcessResult, error) {

	res := newResult(flow.KeyStr, requestID)
	if !e.opts.Cfg.InferGenerationWithoutHandshake {
		return e.rejectNoFlow(ctx, p, res, requestID, seq, diag.CatMissingHandshakeHold,
			"no handshake seen and inference disabled; packet held")
	}
	src := tcpmodel.Endpoint{IP: p.SrcIP, Port: p.SrcPort}
	cs := &connState{flow: flow, client: src}
	g := newGeneration(flow.KeyStr, 1, true, e.opts.Cfg)
	g.birth = seq
	g.connState = stEstablished // cannot prove otherwise; marked inferred
	e.conns[flow.KeyStr] = cs
	cs.gens = append(cs.gens, g)

	dir := tcpmodel.DirC2S
	if p.DirHint == tcpmodel.DirS2C {
		dir = tcpmodel.DirS2C
	}
	d := g.dir(dir)
	d.inferred, d.hasISN = true, true
	d.isn = p.Seq - 1
	d.synAbs = uint64(p.Seq) + seqnum.Space - 1
	d.rcvNxt = d.synAbs + 1
	res.GenIndex, res.Direction = g.index, string(dir)
	res.Inferred = true
	res.SegSeqAbs, res.SegEndAbs = d.synAbs+1, d.synAbs+1+uint64(len(p.Payload))

	e.emit(ctx, e.baseRec(p, res, g, d, requestID, diag.Accepted, diag.CatInferredGeneration,
		"data without handshake: opening inferred generation; absolute offsets unproven"))

	dup, conflicts := d.insert(d.synAbs+1, p.Payload, p.RecordID)
	emits, deltas := d.deliver(ctx, p.RecordID)
	dr := DirectionResult{Direction: string(dir), BytesDedup: dup, Accepted: true}
	e.afterInsert(ctx, g, d, dup, conflicts, emits, deltas, p, requestID, seq, &dr)
	category := diag.CatInferredGeneration
	res.DirResults = append(res.DirResults, dr)
	res.Decision, res.Category = diag.Accepted, category
	if hasUndeliveredConflict(conflicts) {
		res.Decision = diag.Undecidable
	}
	e.persistPacket(ctx, p, g.index, string(dir), p.Seq, p.Seq+uint32(p.SegmentLen()),
		string(res.Decision), string(category), seq)
	e.persistConn(ctx, cs, seq)
	return res, nil
}

// ---- generation binding -----------------------------------------------------------

// bindGeneration finds the generation and direction a non-SYN segment
// belongs to. It scores every live generation by window plausibility and
// binds to the closest match; an unresolved tie is reported as ambiguous
// rather than guessed.
func (e *Engine) bindGeneration(cs *connState, flow tcpmodel.FlowKey,
	p tcpmodel.Packet, isRST bool) (*generation, tcpmodel.Direction, string, bool) {

	dir, reason := e.orient(cs, flow, p)
	if dir == tcpmodel.DirUnknown {
		return nil, dir, reason, false
	}

	type cand struct {
		g      *generation
		dist   int64 // signed distance to the expected window
		closed bool
	}
	var cands []cand
	for i := len(cs.gens) - 1; i >= 0; i-- {
		g := cs.gens[i]
		d := g.dir(dir)
		if d.reset {
			continue
		}
		switch {
		case d.hasISN:
			hint := d.rcvNxt
			if d.finSeen {
				hint = d.finAbs + 1
			}
			abs := seqnum.Extend(p.Seq, hint)
			delta := int64(abs) - int64(hint)
			if int64(absDist(delta)) > int64(seqnum.Space/2) {
				continue
			}
			// Closed generations stay valid bind targets: pure
			// retransmissions and post-FIN junk must reach the direction
			// state so they get the right evidence verdict; live
			// generations win the preference comparison below.
			cands = append(cands, cand{g: g, dist: delta, closed: g.isClosedOrReset()})
		case g.inferred:
			// Direction without a learned ISN in an inferred generation.
			cands = append(cands, cand{g: g, dist: 1 << 40, closed: false})
		}
	}
	if len(cands) == 0 {
		return nil, dir, "segment sequence fits no live generation window", false
	}
	// Prefer live generations over closed ones, then nearest window.
	best := cands[0]
	for _, c := range cands[1:] {
		if (c.closed != best.closed && !c.closed) ||
			(c.closed == best.closed && abs64(c.dist) < abs64(best.dist)) {
			best = c
		}
	}
	// Detect an exact tie between two live generations (ambiguous reuse).
	if !isRST && len(p.Payload) > 0 {
		tied := 0
		for _, c := range cands {
			if !c.closed && abs64(c.dist) == abs64(best.dist) {
				tied++
			}
		}
		if tied > 1 {
			return nil, dir, "sequence equally plausible in multiple generations; refusing to guess", false
		}
	}
	return best.g, dir, "", true
}

func abs64(x int64) int64 {
	if x < 0 {
		return -x
	}
	return x
}

// orient decides packet direction using DirHint, the learned client
// endpoint, or the SYN-acknowledgement field.
func (e *Engine) orient(cs *connState, flow tcpmodel.FlowKey, p tcpmodel.Packet) (tcpmodel.Direction, string) {
	if p.DirHint == tcpmodel.DirC2S || p.DirHint == tcpmodel.DirS2C {
		return p.DirHint, ""
	}
	src := tcpmodel.Endpoint{IP: p.SrcIP, Port: p.SrcPort}
	if cs.client.IsValid() {
		switch {
		case tcpmodel.CompareEndpoints(src, cs.client) == 0:
			return tcpmodel.DirC2S, ""
		case tcpmodel.CompareEndpoints(src, flow.Other(cs.client)) == 0:
			return tcpmodel.DirS2C, ""
		}
	}
	// Fall back to canonical flow orientation.
	d := flow.DirectionOf(p)
	if d == tcpmodel.DirUnknown {
		return d, "source endpoint belongs to neither side of the 4-tuple"
	}
	// Canonical A/B ordering may not equal client/server; without a learned
	// client this is our best orientation (mark via reason only).
	return d, ""
}

// ---- post-insert bookkeeping ------------------------------------------------------

// afterInsert persists emitted chunks, conflicts and gap transitions for
// one inserted segment.
func (e *Engine) afterInsert(ctx context.Context, g *generation, d *dirAssembler,
	dup int, conflicts []conflictEvent, emits []emit, deltas []gapDelta,
	p tcpmodel.Packet, requestID string, ingestSeq int64, dr *DirectionResult) {

	for _, em := range emits {
		dr.Chunks = append(dr.Chunks, DeliveredChunk{
			Direction: d.direction, StreamOff: em.off, Data: em.data, RecordID: em.recordID,
		})
		if e.opts.Store != nil {
			_ = e.opts.Store.InsertChunk(ctx, store.ChunkRow{
				FlowKey: d.flowKey, GenIndex: g.index, Direction: d.direction,
				StreamOff: em.off, Data: em.data, RecordID: em.recordID, IngestSeq: ingestSeq,
			})
		}
	}
	// Gap transitions. A delivered run fills every open gap up to the new
	// frontier; a buffered run that still leaves a hole reconciles the
	// single current gap interval (deduped against the last one observed).
	var frontier uint64
	var hole *[2]uint64
	for _, dl := range deltas {
		if dl.filled {
			frontier = dl.endOff
		}
		if dl.opened {
			g := [2]uint64{dl.startOff, dl.endOff}
			hole = &g
		}
	}
	if e.opts.Store != nil && frontier > 0 {
		// Seed known gaps (covers a process restart before first deliver).
		if d.curGap == nil {
			if existing, err := e.opts.Store.OpenGaps(ctx, d.flowKey, g.index, d.direction); err == nil && len(existing) > 0 {
				d.curGap = &existing[0]
			}
		}
		_ = e.opts.Store.FillGapsUpTo(ctx, d.flowKey, g.index, d.direction, frontier, p.RecordID, ingestSeq)
		d.curGap = nil
	}
	if hole != nil && (d.curGap == nil || *d.curGap != *hole) {
		if e.opts.Store != nil {
			_ = e.opts.Store.ReconcileOpenGap(ctx, d.flowKey, g.index, d.direction,
				(*hole)[0], (*hole)[1], ingestSeq)
		}
		d.curGap = hole
		e.emit(ctx, diag.Record{
			RequestID: requestID, RecordID: p.RecordID, FlowKey: d.flowKey,
			Direction: d.direction, Decision: diag.Undecidable, Category: diag.CatGap,
			Reason:    fmt.Sprintf("missing evidence for stream offsets [%d,%d)", (*hole)[0], (*hole)[1]),
			SegSeqAbs: d.synAbs + 1 + (*hole)[0], SegEndAbs: d.synAbs + 1 + (*hole)[1],
			State: d.state(), Src: ep(p.SrcIP, p.SrcPort), Dst: ep(p.DstIP, p.DstPort),
		})
	}
	for _, c := range conflicts {
		// Events from the buffer already name the maximal genuinely
		// differing sub-ranges; persist each one directly.
		sb := conflictSub{
			startAbs: c.startAbs, endAbs: c.endAbs,
			incumbentSHA: c.incumbentSHA, newcomerSHA: c.newcomerSHA,
			incumbentBytes: c.incumbentBytes, newcomerBytes: c.newcomerBytes,
		}
		e.persistConflict(ctx, g, d, p, requestID, ingestSeq, c, sb, dr)
	}
}

// conflictSub carries the precise contradictory sub-range to persist.
type conflictSub struct {
	startAbs, endAbs uint64
	incumbentSHA     string
	newcomerSHA      string
	incumbentBytes   []byte
	newcomerBytes    []byte
}

// persistConflict records and emits one refined contradictory sub-range.
func (e *Engine) persistConflict(ctx context.Context, g *generation, d *dirAssembler,
	p tcpmodel.Packet, requestID string, ingestSeq int64, c conflictEvent, sb conflictSub,
	dr *DirectionResult) {

	winner := "incumbent"
	cat := diag.CatConflictFirstWins
	status := "resolved-first-wins"
	switch {
	case c.delivered:
		winner, status = "incumbent", "open-delivered-immutable"
		cat = diag.CatConflictDelivered
		if sb.incumbentSHA == "" {
			cat = diag.CatConflictUnverifiable
		}
	case e.opts.Cfg.OverlapPolicy == config.PolicyLastWins:
		winner, status, cat = "newcomer", "resolved-last-wins", diag.CatConflictLastReplaced
	case e.opts.Cfg.OverlapPolicy == config.PolicyQuarantine:
		winner, status, cat = "held", "open-quarantined", diag.CatConflictQuarantined
	}
	if e.opts.Store != nil {
		_ = e.opts.Store.InsertConflict(ctx, store.ConflictRow{
			FlowKey: d.flowKey, GenIndex: g.index, Direction: d.direction,
			StartAbs: sb.startAbs, EndAbs: sb.endAbs,
			StartOff: d.off(sb.startAbs), EndOff: d.off(sb.endAbs),
			IncumbentRecordID: c.incumbentRec, NewcomerRecordID: c.newcomerRec,
			IncumbentSHA: sb.incumbentSHA, NewcomerSHA: sb.newcomerSHA,
			Policy: string(e.opts.Cfg.OverlapPolicy), Winner: winner, Status: status,
			IngestSeq: ingestSeq,
		})
	}
	decision := diag.Rejected
	if c.delivered || e.opts.Cfg.OverlapPolicy == config.PolicyQuarantine {
		decision = diag.Undecidable
	}
	e.emit(ctx, diag.Record{
		RequestID: requestID, RecordID: p.RecordID, FlowKey: d.flowKey,
		Direction: d.direction, Decision: decision, Category: cat,
		Reason: fmt.Sprintf("contradictory bytes at abs [%d,%d) policy=%s winner=%s",
			sb.startAbs, sb.endAbs, e.opts.Cfg.OverlapPolicy, winner),
		SegSeqAbs: sb.startAbs, SegEndAbs: sb.endAbs, State: d.state(),
		Conflict: &diag.Conflict{
			StartAbs: sb.startAbs, EndAbs: sb.endAbs,
			IncumbentSHA: sb.incumbentSHA, NewcomerSHA: sb.newcomerSHA, Winner: winner,
		},
		Src: ep(p.SrcIP, p.SrcPort), Dst: ep(p.DstIP, p.DstPort),
	})
	if decision == diag.Undecidable {
		dr.Accepted = false
	}
	dr.BytesRejected += int(sb.endAbs - sb.startAbs)
}

// ---- small helpers ----------------------------------------------------------------

func newResult(flow, requestID string) ProcessResult {
	return ProcessResult{FlowKey: flow, RequestID: requestID}
}

func ep(ip fmt.Stringer, port uint16) string {
	if ip == nil {
		return ""
	}
	return fmt.Sprintf("%s:%d", ip.String(), port)
}

func (e *Engine) emit(ctx context.Context, r diag.Record) {
	if e.opts.Sink != nil {
		e.opts.Sink.Emit(r)
		return
	}
	if e.opts.Store != nil {
		_ = e.opts.Store.InsertDiagnosticRow(ctx, r, 0)
	}
}

func (e *Engine) baseRec(p tcpmodel.Packet, res ProcessResult, g *generation, d *dirAssembler,
	requestID string, dec diag.Decision, cat diag.Category, reason string) diag.Record {
	return diag.Record{
		RequestID: requestID, RecordID: p.RecordID, FlowKey: res.FlowKey,
		Direction: d.direction, Decision: dec, Category: cat, Reason: reason,
		SegSeqAbs: res.SegSeqAbs, SegEndAbs: res.SegEndAbs, State: d.state(),
		Payload: diag.FingerprintPayload(p.Payload, e.opts.Diag.PayloadPreviewBytes),
		Src:     ep(p.SrcIP, p.SrcPort), Dst: ep(p.DstIP, p.DstPort),
	}
}

func (e *Engine) rejectMalformed(ctx context.Context, p tcpmodel.Packet, requestID string, seq int64, cause error) (ProcessResult, error) {
	r := ProcessResult{Decision: diag.Rejected, Category: diag.CatBadPacket, Reason: cause.Error(), RequestID: requestID}
	e.emit(ctx, diag.Record{RequestID: requestID, RecordID: p.RecordID,
		Decision: diag.Rejected, Category: diag.CatBadPacket, Reason: cause.Error()})
	return r, nil
}

func (e *Engine) rejectNoFlow(ctx context.Context, p tcpmodel.Packet, res ProcessResult,
	requestID string, seq int64, cat diag.Category, reason string) (ProcessResult, error) {
	res.Decision, res.Category, res.Reason = diag.Rejected, cat, reason
	e.emit(ctx, diag.Record{
		RequestID: requestID, RecordID: p.RecordID, FlowKey: res.FlowKey,
		Decision: diag.Rejected, Category: cat, Reason: reason,
		Payload: diag.FingerprintPayload(p.Payload, e.opts.Diag.PayloadPreviewBytes),
		Src:     ep(p.SrcIP, p.SrcPort), Dst: ep(p.DstIP, p.DstPort),
	})
	if e.opts.Store != nil {
		_ = e.opts.Store.InsertPacket(ctx, store.PacketRow{
			RecordID: p.RecordID, Source: captureSource, ObsOrder: p.Order, FlowKey: res.FlowKey,
			Direction: "", Seq32: p.Seq, Ack32: p.Ack,
			SYN: p.SYN, FIN: p.FIN, RST: p.RST, AckFlag: p.ACK,
			Payload: p.Payload, Decision: "REJECTED", Category: string(cat), IngestSeq: seq,
		})
	}
	return res, nil
}

func (e *Engine) persistPacket(ctx context.Context, p tcpmodel.Packet, gen int, dir string,
	segStart, segEnd uint32, decision, category string, seq int64) {
	if e.opts.Store == nil {
		return
	}
	var gp *int
	if gen > 0 {
		g := gen
		gp = &g
	}
	_ = e.opts.Store.InsertPacket(ctx, store.PacketRow{
		RecordID: p.RecordID, Source: captureSource, ObsOrder: p.Order, FlowKey: mustFlow(p),
		GenIndex: gp, Direction: dir,
		SYN: p.SYN, FIN: p.FIN, RST: p.RST, AckFlag: p.ACK,
		Seq32: p.Seq, Ack32: p.Ack,
		SegSeqAbs: uint64(segStart), SegEndAbs: uint64(segEnd),
		Payload: p.Payload, Decision: decision, Category: category, IngestSeq: seq,
	})
}

func mustFlow(p tcpmodel.Packet) string {
	f, err := p.Flow()
	if err != nil {
		return ""
	}
	return f.KeyStr
}

func (e *Engine) persistConn(ctx context.Context, cs *connState, seq int64) {
	if e.opts.Store == nil {
		return
	}
	last := cs.gens[len(cs.gens)-1]
	_ = e.opts.Store.UpsertConnection(ctx, store.ConnectionRow{
		FlowKey:   cs.flow.KeyStr,
		EndpointA: ep2(cs.flow.A), EndpointB: ep2(cs.flow.B),
		ClientEP: ep2(cs.client), State: last.connSummary(),
		CreatedSeq: seq, UpdatedSeq: seq,
	})
	for _, g := range cs.gens {
		c2s, s2c := g.stateLabels()
		row := store.GenerationRow{
			FlowKey: cs.flow.KeyStr, GenIndex: g.index, Inferred: g.inferred,
			C2SState: c2s, S2CState: s2c, CreatedSeq: seq, UpdatedSeq: seq,
		}
		attach := func(d *dirAssembler) (*uint32, *uint64, uint64, *uint64) {
			var isn *uint32
			if d.hasISN {
				v := d.isn
				isn = &v
			}
			var nxt *uint64
			if d.hasISN {
				v := d.rcvNxt
				nxt = &v
			}
			var fin *uint64
			if d.finSeen {
				v := d.finAbs
				fin = &v
			}
			return isn, nxt, d.deliveredOffset(), fin
		}
		isnA, nxtA, delA, finA := attach(g.c2s)
		isnB, nxtB, delB, finB := attach(g.s2c)
		row.C2SISN, row.C2SRcvNxtAbs, row.C2SDelivered, row.C2SFineEndAbs = isnA, nxtA, delA, finA
		row.S2CISN, row.S2CRcvNxtAbs, row.S2CDelivered, row.S2CFineEndAbs = isnB, nxtB, delB, finB
		// Idempotent upsert: the same generation is touched by the SYN and
		// by every later data packet.
		_ = e.opts.Store.UpsertGeneration(ctx, row)
	}
}

func ep2(x tcpmodel.Endpoint) string {
	if !x.IP.IsValid() {
		return ""
	}
	return fmt.Sprintf("%s:%d", x.IP.String(), x.Port)
}

func conflictCategory(cs []conflictEvent) diag.Category {
	for _, c := range cs {
		if c.delivered {
			if c.incumbentSHA == "" {
				return diag.CatConflictUnverifiable
			}
			return diag.CatConflictDelivered
		}
	}
	return diag.CatConflictFirstWins
}

func hasUndeliveredConflict(cs []conflictEvent) bool {
	for _, c := range cs {
		if c.delivered {
			return true
		}
	}
	return false
}

func verdictReason(dup int, cs []conflictEvent, emits []emit, dr DirectionResult) string {
	switch {
	case len(cs) > 0:
		return fmt.Sprintf("%d conflicting byte ranges; %d duplicate bytes handled", len(cs), dup)
	case dup > 0:
		return fmt.Sprintf("identical retransmission: %d bytes deduplicated", dup)
	case len(emits) > 0:
		return "in-order data delivered"
	default:
		return "out-of-order data buffered pending missing bytes"
	}
}

func absDist(x int64) uint64 {
	if x < 0 {
		return uint64(-x)
	}
	return uint64(x)
}
