package reassembly

import (
	"fmt"
	"sync"

	"tcpreplay/internal/netmodel"
)

// previewMax is the hard cap on redacted payload previews in events.
const previewMax = 16

// DirectionState holds one direction of one connection generation.
type DirectionState struct {
	mapper  netmodel.SeqMapper
	buf     *sparseBuffer
	base    int64 // coordinate of the first possible data byte (1 after SYN, or 0)
	finPos  int64 // FIN coordinate; -1 until observed
	finSeen bool
	reset   bool
	// firstObservedSeq / handshakeKnown describe coordinate provenance.
	firstObservedSeq uint32
	handshakeKnown   bool // SYN for this direction was observed
	anchored         bool // at least one data/control position mapped
	// quarantined fragments under policy=quarantine, per offering record.
	held []HeldByte
}

func newDirectionState() *DirectionState {
	return &DirectionState{buf: newSparseBuffer(), finPos: -1}
}

// nextContiguous is the first coordinate missing in the accepted prefix.
func (d *DirectionState) nextContiguous() int64 {
	hi, _ := d.buf.contiguous(d.base)
	return hi
}

// extentEnd is one past the highest accepted byte, or base when empty.
func (d *DirectionState) extentEnd() int64 {
	max := d.base
	for off := range d.buf.cells {
		if off+1 > max {
			max = off + 1
		}
	}
	if d.finSeen && d.finPos > max {
		max = d.finPos
	}
	return max
}

// Generation is one incarnation of a 4-tuple: between the SYN that opened it
// and its FIN/FIN or RST close. Reused ports with a later SYN create a new
// generation instead of corrupting an earlier stream.
type Generation struct {
	Flow  netmodel.FlowKey
	Index int // 0-based within the flow
	AtoB  *DirectionState
	BtoA  *DirectionState
	// Role labels, assigned from handshake direction; empty until known.
	clientIsA   bool // canonical A sent the opening SYN
	rolesSet    bool
	established bool // SYN+ACK observed
	closed      bool // fully closed (both FIN or RST)
	reset       bool
}

func (g *Generation) dir(aToB bool) *DirectionState {
	if aToB {
		if g.AtoB == nil {
			g.AtoB = newDirectionState()
		}
		return g.AtoB
	}
	if g.BtoA == nil {
		g.BtoA = newDirectionState()
	}
	return g.BtoA
}

// ProcessResult is everything one packet produced.
type ProcessResult struct {
	GenerationIndex int
	Direction       string // "a_to_b" | "b_to_a"
	Accepted        bool
	Put             PutResult
	Events          []Event
	Conflicts       []Conflict
	FlowClosed      bool
}

// Manager is the reassembly state machine. It is safe for concurrent use;
// ordering within one ingest request is preserved by the service.
type Manager struct {
	mu          sync.Mutex
	flows       map[netmodel.FlowKey][]*Generation
	policy      OverlapPolicy
	preview     bool
	seq         int64
	conflictSeq int64
	conflicts   []Conflict
}

// NewManager builds a manager with the given overlap policy.
func NewManager(policy OverlapPolicy, enablePreview bool) *Manager {
	if !ValidPolicy(policy) {
		policy = PolicyQuarantine
	}
	return &Manager{
		flows:   map[netmodel.FlowKey][]*Generation{},
		policy:  policy,
		preview: enablePreview,
	}
}

// Policy returns the active overlap policy.
func (m *Manager) Policy() OverlapPolicy { return m.policy }

func (m *Manager) emit(reqID, recordID, ts string, g *Generation, aToB bool, code EventCode, level EventLevel, msg string, d *DirectionState, raw uint32, start, end int64) Event {
	m.seq++
	dn := "a_to_b"
	if !aToB {
		dn = "b_to_a"
	}
	e := Event{
		Seq: m.seq, RequestID: reqID, RecordID: recordID, Timestamp: ts,
		Code: code, Level: level, Flow: g.Flow.String(), Generation: g.Index,
		Direction: dn, Msg: msg, RawSeq: raw,
		AbsStart: start, AbsEnd: end, FINPos: -1,
	}
	if d != nil {
		e.NextContig = d.nextContiguous() - d.base
		if d.finSeen {
			e.FINPos = d.finPos - d.base
		} else {
			e.FINPos = -1
		}
	}
	return e
}

// maskPreview renders at most previewMax bytes for diagnostics; the rest is
// only counted. It is used solely for the explicitly-enabled preview path.
func maskPreview(p []byte) (string, int) {
	if len(p) == 0 {
		return "", 0
	}
	n := len(p)
	if n > previewMax {
		p = p[:previewMax]
	}
	const hexDigits = "0123456789abcdef"
	out := make([]byte, 0, len(p)*3)
	for i, b := range p {
		if i > 0 {
			out = append(out, ' ')
		}
		out = append(out, hexDigits[b>>4], hexDigits[b&0xf])
	}
	return string(out), n
}

// genFlow returns all generations for a flow.
func (m *Manager) genFlow(k netmodel.FlowKey) []*Generation { return m.flows[k] }

// latestOrNewGeneration resolves which generation a packet belongs to and
// creates a fresh one on a new opening SYN.
func (m *Manager) latestOrNewGeneration(pk netmodel.Packet, k netmodel.FlowKey, aToB bool, reqID, ts string) (*Generation, []Event) {
	gens := m.flows[k]
	isSYN := pk.Has(netmodel.FlagSYN)
	isACK := pk.Has(netmodel.FlagACK)

	if isSYN && !isACK {
		// Opening SYN: if it belongs to a new incarnation (previous gen closed
		// or reset, or this ISN differs from the current open gen's opening
		// ISN), start a new generation.
		if len(gens) > 0 {
			cur := gens[len(gens)-1]
			opening := cur.dir(aToB)
			if cur.closed || cur.reset || (opening.handshakeKnown && opening.firstObservedSeq != pk.Seq) {
				ng := m.newGeneration(k, len(gens))
				gens = append(gens, ng)
				m.flows[k] = gens
				ev := m.emit(reqID, pk.RecordID, ts, ng, aToB, EvNewGeneration, LevelInfo,
					fmt.Sprintf("SYN on reused 4-tuple opens generation #%d", ng.Index), nil, pk.Seq, 0, 0)
				return ng, []Event{ev}
			}
			// Same open generation, same ISN: duplicate/retransmitted SYN.
			ev := m.emit(reqID, pk.RecordID, ts, cur, aToB, EvSYNDuplicate, LevelInfo,
				"duplicate SYN with matching ISN; treated as retransmission", opening, pk.Seq, 0, 0)
			return cur, []Event{ev}
		}
		ng := m.newGeneration(k, 0)
		m.flows[k] = append(gens, ng)
		ev := m.emit(reqID, pk.RecordID, ts, ng, aToB, EvSYNOpened, LevelInfo,
			"opening SYN creates generation #0", nil, pk.Seq, 0, 0)
		return ng, []Event{ev}
	}

	if len(gens) == 0 {
		// Data/control without any handshake evidence: anchorless generation.
		ng := m.newGeneration(k, 0)
		m.flows[k] = []*Generation{ng}
		ev := m.emit(reqID, pk.RecordID, ts, ng, aToB, EvHandshakeAbsent, LevelUndecided,
			"no SYN observed for 4-tuple; offsets relative to first seen byte, ISN unknown", nil, pk.Seq, 0, 0)
		return ng, []Event{ev}
	}
	return gens[len(gens)-1], nil
}

func (m *Manager) newGeneration(k netmodel.FlowKey, idx int) *Generation {
	return &Generation{Flow: k, Index: idx}
}
